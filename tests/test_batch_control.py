import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

from batch_control import BatchControlService


class BatchControlTestBase(unittest.TestCase):
    """准备主体、角色、配方与已检验可用的原料批次。"""

    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.batch = BatchControlService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="茶道转化中心")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, role in (
                ("wh", "wh1", "warehouse"), ("qc1", "qc1", "qc"), ("qc2", "qc2", "qc"),
                ("fac", "f1", "factory"), ("brand", "b1", "brand"), ("aud", "au1", "auditor")):
            self.domain.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                       display_name=actor_id, role=role, organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="试产基地", timezone_name="Asia/Shanghai")
        recipe = self.batch.create_recipe(
            request_id="recipe", actor_id="a1", product_code="GIFT-SET", version=1,
            lines=[{"material_code": "TEA", "quantity": 0.25, "unit": "kg",
                    "substitutes": ["TEA-ALT"]},
                   {"material_code": "GLAZE", "quantity": 0.05, "unit": "kg"},
                   {"material_code": "PACK", "quantity": 1, "unit": "套"}])
        self.recipe_id = recipe["recipe_id"]
        self.batch.activate_recipe(request_id="recipe-act", actor_id="a1", recipe_id=self.recipe_id)
        self._lot("LOT-TEA", "TEA", 100, "kg")
        self._lot("LOT-ALT", "TEA-ALT", 50, "kg")
        self._lot("LOT-GLAZE", "GLAZE", 40, "kg")
        self._lot("LOT-PACK", "PACK", 200, "套")

    def tearDown(self):
        self.database.close()

    def _lot(self, lot_id, code, quantity, unit):
        self.batch.register_material_lot(
            request_id=f"lot-{lot_id}", actor_id="wh1", site_id="s1", lot_id=lot_id,
            material_code=code, material_name=code, origin="产地", supplier_name="供应商",
            quantity=quantity, unit=unit,
            certificates=[{"cert_type": "产地证明", "cert_number": f"CO-{lot_id}",
                           "issuer": "协会", "issued_on": "2026-09-20",
                           "file_hash": f"h-{lot_id}"}])
        self.batch.record_inspection(request_id=f"insp-{lot_id}", actor_id="qc1",
                                     target_type="material_lot", target_id=lot_id,
                                     item="常规检验", method="抽检", standard="企标",
                                     result="pass")

    def _order(self, order_id="WO-1", planned=2):
        result = self.batch.create_work_order(
            request_id=f"wo-{order_id}", actor_id="f1", order_id=order_id, site_id="s1",
            product_code="GIFT-SET", planned_quantity=planned, unit="套",
            steps=[{"name": "配料", "unit": "kg"}, {"name": "分装", "unit": "kg"},
                   {"name": "包装", "unit": "套"}])
        return result["step_ids"]

    def _release(self, prefix, target_type, target_id):
        self.batch.approve_release(request_id=f"{prefix}-1", actor_id="qc1",
                                   target_type=target_type, target_id=target_id)
        return self.batch.approve_release(request_id=f"{prefix}-2", actor_id="qc2",
                                          target_type=target_type, target_id=target_id)

    def _component_id(self, serial):
        row = self.database.connection.execute(
            "SELECT component_id FROM components WHERE serial_no=?", (serial,)).fetchone()
        return row["component_id"]

    def _unit_id(self, serial):
        row = self.database.connection.execute(
            "SELECT unit_id FROM finished_units WHERE serial_no=?", (serial,)).fetchone()
        return row["unit_id"]

    def _blend_component(self, order_id, step_ids, serial="BLEND-1", qty=4.5, request_tag=""):
        """生产一个已检验、已双人放行的配料组件。"""

        self.batch.issue_material(request_id=f"i{request_tag}-{serial}", actor_id="wh1",
                                  order_id=order_id, step_id=step_ids[0],
                                  lot_id="LOT-TEA", quantity=5)
        self.batch.report_output(request_id=f"o{request_tag}-{serial}", actor_id="f1",
                                 order_id=order_id, step_id=step_ids[0], good_quantity=qty,
                                 scrap_quantity=0.5,
                                 outputs=[{"serial_no": serial, "component_code": "MIX",
                                           "quantity": qty}])
        component_id = self._component_id(serial)
        self.batch.record_inspection(request_id=f"c{request_tag}-{serial}", actor_id="qc1",
                                     target_type="component", target_id=component_id,
                                     item="均匀度", method="抽样", standard="企标", result="pass")
        self._release(f"r{request_tag}-{serial}", "component", component_id)
        return component_id


class FullFlowTest(BatchControlTestBase):
    def test_full_flow_conservation_and_gating(self):
        s1, s2, s3 = self._order("WO-1", planned=2)
        # 领料与替代料。
        self.batch.issue_material(request_id="i1", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=8)
        sub = self.batch.issue_material(request_id="i2", actor_id="wh1", order_id="WO-1",
                                        step_id=s1, lot_id="LOT-ALT", quantity=2,
                                        substituted_for="TEA")
        self.assertEqual("substitute", sub["kind"])
        # 工序产出：投入 10 = 合格 8.5 + 报废 1 + 返工 0.5 + 在制 0。
        self.batch.report_output(request_id="o1", actor_id="f1", order_id="WO-1", step_id=s1,
                                 good_quantity=8.5, scrap_quantity=1, rework_quantity=0.5,
                                 outputs=[{"serial_no": "BLEND-1", "component_code": "MIX",
                                           "quantity": 8.5}])
        state = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        step1 = state["steps"][0]
        self.assertEqual((10, 0, 8.5, 1, 0.5, 0),
                         (step1["received"], step1["returned"], step1["good"],
                          step1["scrap"], step1["rework"], step1["wip"]))
        self.assertTrue(state["balanced"])
        # 匿名返工数量补回合格。
        self.batch.resolve_rework(request_id="rw1", actor_id="f1", order_id="WO-1", step_id=s1,
                                  quantity=0.5, outcome="good",
                                  outputs=[{"serial_no": "BLEND-1R", "component_code": "MIX",
                                            "quantity": 0.5}])
        # 未放行组件不能进入下一工序。
        blend = self._component_id("BLEND-1")
        self.batch.record_inspection(request_id="ci1", actor_id="qc1", target_type="component",
                                     target_id=blend, item="均匀度", method="抽样",
                                     standard="企标", result="pass")
        with self.assertRaises(ConflictError):
            self.batch.report_output(request_id="o2x", actor_id="f1", order_id="WO-1", step_id=s2,
                                     good_quantity=1,
                                     outputs=[{"serial_no": "TB-X", "component_code": "BAG",
                                               "quantity": 1}],
                                     inputs=[{"component_id": blend, "quantity": 1}])
        # 双人放行后才能消耗。
        self._release("rb1", "component", blend)
        blend_r = self._component_id("BLEND-1R")
        self.batch.record_inspection(request_id="ci2", actor_id="qc1", target_type="component",
                                     target_id=blend_r, item="均匀度", method="抽样",
                                     standard="企标", result="pass")
        self._release("rb2", "component", blend_r)
        self.batch.report_output(request_id="o2", actor_id="f1", order_id="WO-1", step_id=s2,
                                 good_quantity=8.6, scrap_quantity=0.4,
                                 outputs=[{"serial_no": "TB-1", "component_code": "BAG",
                                           "quantity": 8.6}],
                                 inputs=[{"component_id": blend, "quantity": 8.5},
                                         {"component_id": blend_r, "quantity": 0.5}])
        bag = self._component_id("TB-1")
        self.batch.record_inspection(request_id="ci3", actor_id="qc1", target_type="component",
                                     target_id=bag, item="农残", method="GC-MS",
                                     standard="GB 2763", result="pass", kind="additional")
        self._release("rb3", "component", bag)
        # 包装组合。
        self.batch.issue_material(request_id="i3", actor_id="wh1", order_id="WO-1",
                                  step_id=s3, lot_id="LOT-PACK", quantity=2)
        output = self.batch.report_output(request_id="o3", actor_id="f1", order_id="WO-1",
                                          step_id=s3, good_quantity=2,
                                          outputs=[{"serial_no": "U-1"}, {"serial_no": "U-2"}],
                                          inputs=[{"component_id": bag, "quantity": 8.6}])
        self.batch.record_inspection(request_id="ci4", actor_id="qc1",
                                     target_type="finished_batch", target_id=output["output_id"],
                                     item="标签", method="全检", standard="企标", result="pass")
        self._release("rb4", "finished_batch", output["output_id"])
        # 销售与渠道发货。
        self.batch.create_sales_order(request_id="so1", actor_id="b1", sales_order_id="SO-1",
                                      channel="天猫", serials=["U-1", "U-2"])
        self.batch.ship_order(request_id="sh1", actor_id="b1", sales_order_id="SO-1",
                              batch_id="CB-1")
        self.assertEqual("shipped", self.batch.get_unit(actor_id="au1", serial_no="U-1")["status"])
        final = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        self.assertTrue(final["balanced"])
        valid, _ = self.domain.verify_audit()
        self.assertTrue(valid)


class IssueTest(BatchControlTestBase):
    def test_repeated_scan_does_not_deduct_twice(self):
        s1, _, _ = self._order()
        first = self.batch.issue_material(request_id="scan-1", actor_id="wh1", order_id="WO-1",
                                          step_id=s1, lot_id="LOT-TEA", quantity=10)
        replay = self.batch.issue_material(request_id="scan-1", actor_id="wh1", order_id="WO-1",
                                           step_id=s1, lot_id="LOT-TEA", quantity=10)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["issue_id"], replay["issue_id"])
        self.assertEqual(90, self.batch.get_lot(actor_id="au1", lot_id="LOT-TEA")["quantity_on_hand"])

    def test_same_request_id_with_different_payload_conflicts(self):
        s1, _, _ = self._order()
        self.batch.issue_material(request_id="scan-2", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=10)
        with self.assertRaises(ConflictError):
            self.batch.issue_material(request_id="scan-2", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-TEA", quantity=11)

    def test_insufficient_stock_and_unavailable_lot_rejected(self):
        s1, _, _ = self._order()
        with self.assertRaises(ConflictError):
            self.batch.issue_material(request_id="i-over", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-TEA", quantity=101)
        self.batch.record_inspection(request_id="bad", actor_id="qc1",
                                     target_type="material_lot", target_id="LOT-GLAZE",
                                     item="重金属", method="ICP", standard="国标", result="fail")
        with self.assertRaises(ConflictError):
            self.batch.issue_material(request_id="i-q", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-GLAZE", quantity=1)

    def test_concurrent_issues_never_go_negative(self):
        s1, _, _ = self._order()
        errors = []
        successes = []

        def worker(index):
            try:
                self.batch.issue_material(request_id=f"conc-{index}", actor_id="wh1",
                                          order_id="WO-1", step_id=s1,
                                          lot_id="LOT-TEA", quantity=10)
                successes.append(index)
            except ConflictError:
                errors.append(index)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(10, len(successes))
        self.assertEqual(2, len(errors))
        lot = self.batch.get_lot(actor_id="au1", lot_id="LOT-TEA")
        self.assertEqual(0, lot["quantity_on_hand"])
        self.assertGreaterEqual(lot["quantity_on_hand"], 0)

    def test_return_material_restores_stock_and_conservation(self):
        s1, _, _ = self._order()
        self.batch.issue_material(request_id="i1", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=10)
        self.batch.return_material(request_id="rt1", actor_id="wh1", order_id="WO-1",
                                   step_id=s1, lot_id="LOT-TEA", quantity=4)
        lot = self.batch.get_lot(actor_id="au1", lot_id="LOT-TEA")
        self.assertEqual(94, lot["quantity_on_hand"])
        state = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        step = state["steps"][0]
        self.assertEqual((10, 4, 6), (step["received"], step["returned"], step["wip"]))
        with self.assertRaises(ConflictError):
            self.batch.return_material(request_id="rt2", actor_id="wh1", order_id="WO-1",
                                       step_id=s1, lot_id="LOT-TEA", quantity=7)


class ReleaseGateTest(BatchControlTestBase):
    def test_dual_release_requires_two_distinct_actors(self):
        s1, _, _ = self._order()
        blend = self._blend_component("WO-1", (s1, None, None))
        component_id = self._component_id("BLEND-1")
        # _blend_component 已完成放行；新建一个未放行组件验证双人规则。
        self.batch.issue_material(request_id="i-x", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=2)
        self.batch.report_output(request_id="o-x", actor_id="f1", order_id="WO-1", step_id=s1,
                                 good_quantity=2,
                                 outputs=[{"serial_no": "BLEND-9", "component_code": "MIX",
                                           "quantity": 2}])
        target = self._component_id("BLEND-9")
        self.batch.record_inspection(request_id="ci-x", actor_id="qc1", target_type="component",
                                     target_id=target, item="均匀度", method="抽样",
                                     standard="企标", result="pass")
        first = self.batch.approve_release(request_id="rl-1", actor_id="qc1",
                                           target_type="component", target_id=target)
        self.assertEqual("pending_first", first["status"])
        with self.assertRaises(ValidationError):
            self.batch.approve_release(request_id="rl-2", actor_id="qc1",
                                       target_type="component", target_id=target)
        second = self.batch.approve_release(request_id="rl-3", actor_id="qc2",
                                            target_type="component", target_id=target)
        self.assertEqual("released", second["status"])
        with self.assertRaises(ConflictError):
            self.batch.approve_release(request_id="rl-4", actor_id="qc2",
                                       target_type="component", target_id=target)

    def test_failed_inspection_blocks_release_and_quarantines(self):
        s1, _, _ = self._order()
        self.batch.issue_material(request_id="i-f", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=2)
        self.batch.report_output(request_id="o-f", actor_id="f1", order_id="WO-1", step_id=s1,
                                 good_quantity=2,
                                 outputs=[{"serial_no": "BLEND-F", "component_code": "MIX",
                                           "quantity": 2}])
        target = self._component_id("BLEND-F")
        with self.assertRaises(ConflictError):
            self.batch.approve_release(request_id="rl-f", actor_id="qc1",
                                       target_type="component", target_id=target)
        self.batch.record_inspection(request_id="ci-f", actor_id="qc1", target_type="component",
                                     target_id=target, item="异物", method="目检",
                                     standard="企标", result="fail")
        self.assertEqual("quarantined",
                         self.batch.get_component(actor_id="au1", component_id=target)["status"])
        with self.assertRaises(ConflictError):
            self.batch.approve_release(request_id="rl-f2", actor_id="qc1",
                                       target_type="component", target_id=target)

    def test_additional_sampling_blocks_released_component(self):
        s1, s2, _ = self._order()
        blend = self._blend_component("WO-1", (s1, s2, None))
        # 追加抽检判定不合格 → 已放行组件被隔离且放行失效。
        self.batch.record_inspection(request_id="ci-add", actor_id="qc1",
                                     target_type="component", target_id=blend,
                                     item="追加抽检-农残", method="GC-MS", standard="GB 2763",
                                     result="fail", kind="additional")
        self.assertEqual("quarantined",
                         self.batch.get_component(actor_id="au1", component_id=blend)["status"])
        with self.assertRaises(ConflictError):
            self.batch.report_output(request_id="o-blocked", actor_id="f1", order_id="WO-1",
                                     step_id=s2, good_quantity=1,
                                     outputs=[{"serial_no": "TB-B", "component_code": "BAG",
                                               "quantity": 1}],
                                     inputs=[{"component_id": blend, "quantity": 1}])

    def test_recipe_version_gating(self):
        s1, _, _ = self._order("WO-1")
        self.batch.issue_material(request_id="i-v", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=5)
        # 配方升版后，锁定旧版本的工单不能继续报工。
        recipe2 = self.batch.create_recipe(
            request_id="recipe2", actor_id="a1", product_code="GIFT-SET", version=2,
            lines=[{"material_code": "TEA", "quantity": 0.3, "unit": "kg"},
                   {"material_code": "GLAZE", "quantity": 0.05, "unit": "kg"},
                   {"material_code": "PACK", "quantity": 1, "unit": "套"}])
        self.batch.activate_recipe(request_id="recipe2-act", actor_id="a1",
                                   recipe_id=recipe2["recipe_id"])
        with self.assertRaises(ConflictError):
            self.batch.report_output(request_id="o-v", actor_id="f1", order_id="WO-1", step_id=s1,
                                     good_quantity=4,
                                     outputs=[{"serial_no": "BLEND-V", "component_code": "MIX",
                                               "quantity": 4}])
        with self.assertRaises(ConflictError):
            self.batch.issue_material(request_id="i-v2", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-ALT", quantity=1,
                                      substituted_for="TEA")

    def test_substitute_validation(self):
        s1, _, _ = self._order()
        with self.assertRaises(ValidationError):
            self.batch.issue_material(request_id="i-s1", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-GLAZE", quantity=1,
                                      substituted_for="TEA")
        with self.assertRaises(ValidationError):
            self.batch.issue_material(request_id="i-s2", actor_id="wh1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-ALT", quantity=1)


class ReworkAndUnpackTest(BatchControlTestBase):
    def test_component_rework_keeps_genealogy_and_requires_re_release(self):
        s1, s2, _ = self._order()
        blend = self._blend_component("WO-1", (s1, s2, None))
        # 追加抽检不合格 → 隔离 → 返工 → 修复回待检。
        self.batch.record_inspection(request_id="ci-rw", actor_id="qc1",
                                     target_type="component", target_id=blend,
                                     item="追加抽检", method="抽样", standard="企标",
                                     result="fail", kind="additional")
        self.batch.rework_component(request_id="rw-c1", actor_id="f1", component_id=blend,
                                    note="重新均质")
        self.assertEqual("rework",
                         self.batch.get_component(actor_id="au1", component_id=blend)["status"])
        state = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        self.assertEqual(4.5, state["steps"][0]["rework"])
        self.batch.resolve_component_rework(request_id="rw-c2", actor_id="f1",
                                            component_id=blend, outcome="good")
        component = self.batch.get_component(actor_id="au1", component_id=blend)
        self.assertEqual("wip", component["status"])
        # 返工后必须重新检验与双人放行才能进入下一工序。
        with self.assertRaises(ConflictError):
            self.batch.report_output(request_id="o-rw", actor_id="f1", order_id="WO-1", step_id=s2,
                                     good_quantity=1,
                                     outputs=[{"serial_no": "TB-R", "component_code": "BAG",
                                               "quantity": 1}],
                                     inputs=[{"component_id": blend, "quantity": 1}])
        self.batch.record_inspection(request_id="ci-rw2", actor_id="qc1",
                                     target_type="component", target_id=blend,
                                     item="复检", method="抽样", standard="企标", result="pass")
        self._release("rl-rw", "component", blend)
        self.batch.report_output(request_id="o-rw2", actor_id="f1", order_id="WO-1", step_id=s2,
                                 good_quantity=4.5,
                                 outputs=[{"serial_no": "TB-R", "component_code": "BAG",
                                           "quantity": 4.5}],
                                 inputs=[{"component_id": blend, "quantity": 4.5}])
        # 原始谱系保留：追溯仍能看到 LOT-TEA 与返工边。
        trace = self.batch.trace_backward(actor_id="au1", target_type="component",
                                          target_id=self._component_id("TB-R"))
        lots = set()

        def walk(node):
            if node.get("type") == "material_lot":
                lots.add(node["id"])
            for parent in node.get("parents", []):
                walk(parent["node"])

        walk(trace)
        self.assertIn("LOT-TEA", lots)
        edge = self.database.connection.execute(
            "SELECT relation FROM genealogy_edges WHERE parent_type='component' AND parent_id=? "
            "AND relation='rework'", (blend,)).fetchone()
        self.assertIsNotNone(edge)
        self.assertTrue(self.batch.verify_conservation(actor_id="au1", order_id="WO-1")["balanced"])

    def test_rework_resolve_to_scrap(self):
        s1, _, _ = self._order()
        blend = self._blend_component("WO-1", (s1, None, None))
        self.batch.record_inspection(request_id="ci-rs", actor_id="qc1",
                                     target_type="component", target_id=blend,
                                     item="追加抽检", method="抽样", standard="企标",
                                     result="fail", kind="additional")
        self.batch.rework_component(request_id="rw-s1", actor_id="f1", component_id=blend)
        self.batch.resolve_component_rework(request_id="rw-s2", actor_id="f1",
                                            component_id=blend, outcome="scrap")
        component = self.batch.get_component(actor_id="au1", component_id=blend)
        self.assertEqual("scrapped", component["status"])
        state = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        # 报废 = 报工时的 0.5 + 返工转报废的 4.5。
        self.assertEqual(5.0, state["steps"][0]["scrap"])
        self.assertTrue(state["balanced"])

    def test_unpack_and_repack_preserves_genealogy(self):
        s1, s2, s3 = self._order()
        blend = self._blend_component("WO-1", (s1, s2, None))
        self.batch.report_output(request_id="o-u1", actor_id="f1", order_id="WO-1", step_id=s2,
                                 good_quantity=4.5,
                                 outputs=[{"serial_no": "TB-U", "component_code": "BAG",
                                           "quantity": 4.5}],
                                 inputs=[{"component_id": blend, "quantity": 4.5}])
        bag = self._component_id("TB-U")
        self.batch.record_inspection(request_id="ci-u", actor_id="qc1", target_type="component",
                                     target_id=bag, item="农残", method="GC-MS",
                                     standard="国标", result="pass")
        self._release("rl-u", "component", bag)
        self.batch.issue_material(request_id="i-u", actor_id="wh1", order_id="WO-1",
                                  step_id=s3, lot_id="LOT-PACK", quantity=2)
        output = self.batch.report_output(request_id="o-u2", actor_id="f1", order_id="WO-1",
                                          step_id=s3, good_quantity=2,
                                          outputs=[{"serial_no": "U-A"}, {"serial_no": "U-B"}],
                                          inputs=[{"component_id": bag, "quantity": 4.5}])
        self.assertEqual("consumed",
                         self.batch.get_component(actor_id="au1", component_id=bag)["status"])
        # 拆包：成品注销、组件回到已放行库存。
        unpacked = self.batch.unpack_output(request_id="un1", actor_id="wh1",
                                            output_id=output["output_id"])
        self.assertEqual(1, len(unpacked["restored"]))
        self.assertEqual("released",
                         self.batch.get_component(actor_id="au1", component_id=bag)["status"])
        self.assertEqual("unpacked",
                         self.batch.get_unit(actor_id="au1", serial_no="U-A")["status"])
        # 重组：同一批组件再次包装，谱系边标记为 repack。
        self.batch.issue_material(request_id="i-u2", actor_id="wh1", order_id="WO-1",
                                  step_id=s3, lot_id="LOT-PACK", quantity=2)
        repacked = self.batch.report_output(request_id="o-u3", actor_id="f1", order_id="WO-1",
                                            step_id=s3, good_quantity=2,
                                            outputs=[{"serial_no": "U-C"}, {"serial_no": "U-D"}],
                                            inputs=[{"component_id": bag, "quantity": 4.5}])
        edge = self.database.connection.execute(
            "SELECT relation FROM genealogy_edges WHERE parent_type='component' AND parent_id=? "
            "AND child_id=? AND relation='repack'",
            (bag, repacked["output_id"])).fetchone()
        self.assertIsNotNone(edge)
        split = self.database.connection.execute(
            "SELECT relation FROM genealogy_edges WHERE child_type='component' AND child_id=? "
            "AND relation='split'", (bag,)).fetchone()
        self.assertIsNotNone(split)
        self.assertTrue(self.batch.verify_conservation(actor_id="au1", order_id="WO-1")["balanced"])


class FreezeAndTraceTest(BatchControlTestBase):
    """构造两个工单：WO-1 使用替代料，WO-2 只用合格主料。"""

    def _build_two_orders(self):
        # WO-1：替代料工单。
        s1, s2, s3 = self._order("WO-1", planned=2)
        self.batch.issue_material(request_id="i-t1", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-TEA", quantity=3)
        self.batch.issue_material(request_id="i-t2", actor_id="wh1", order_id="WO-1",
                                  step_id=s1, lot_id="LOT-ALT", quantity=2,
                                  substituted_for="TEA")
        self.batch.report_output(request_id="o-t1", actor_id="f1", order_id="WO-1", step_id=s1,
                                 good_quantity=4.5, scrap_quantity=0.5,
                                 outputs=[{"serial_no": "BLEND-T1", "component_code": "MIX",
                                           "quantity": 4.5}])
        blend1 = self._component_id("BLEND-T1")
        self.batch.record_inspection(request_id="ci-t1", actor_id="qc1",
                                     target_type="component", target_id=blend1,
                                     item="均匀度", method="抽样", standard="企标", result="pass")
        self._release("rl-t1", "component", blend1)
        self.batch.report_output(request_id="o-t2", actor_id="f1", order_id="WO-1", step_id=s2,
                                 good_quantity=4.5,
                                 outputs=[{"serial_no": "TB-T1", "component_code": "BAG",
                                           "quantity": 4.5}],
                                 inputs=[{"component_id": blend1, "quantity": 4.5}])
        bag1 = self._component_id("TB-T1")
        self.batch.record_inspection(request_id="ci-t2", actor_id="qc1",
                                     target_type="component", target_id=bag1,
                                     item="农残", method="GC-MS", standard="国标", result="pass")
        self._release("rl-t2", "component", bag1)
        self.batch.issue_material(request_id="i-t3", actor_id="wh1", order_id="WO-1",
                                  step_id=s3, lot_id="LOT-PACK", quantity=2)
        out1 = self.batch.report_output(request_id="o-t3", actor_id="f1", order_id="WO-1",
                                        step_id=s3, good_quantity=2,
                                        outputs=[{"serial_no": "U-T1"}, {"serial_no": "U-T2"}],
                                        inputs=[{"component_id": bag1, "quantity": 4.5}])
        self.batch.record_inspection(request_id="ci-t3", actor_id="qc1",
                                     target_type="finished_batch", target_id=out1["output_id"],
                                     item="标签", method="全检", standard="企标", result="pass")
        self._release("rl-t3", "finished_batch", out1["output_id"])
        self.batch.create_sales_order(request_id="so-t1", actor_id="b1", sales_order_id="SO-T1",
                                      channel="天猫", serials=["U-T1"])
        self.batch.ship_order(request_id="sh-t1", actor_id="b1", sales_order_id="SO-T1",
                              batch_id="CB-T1")
        # WO-2：干净工单。
        t1, t2, t3 = self._order("WO-2", planned=1)
        blend2 = self._blend_component("WO-2", (t1, t2, None), serial="BLEND-T2", qty=4.5,
                                       request_tag="t2")
        self.batch.report_output(request_id="o-t4", actor_id="f1", order_id="WO-2", step_id=t2,
                                 good_quantity=4.5,
                                 outputs=[{"serial_no": "TB-T2", "component_code": "BAG",
                                           "quantity": 4.5}],
                                 inputs=[{"component_id": blend2, "quantity": 4.5}])
        bag2 = self._component_id("TB-T2")
        self.batch.record_inspection(request_id="ci-t4", actor_id="qc1",
                                     target_type="component", target_id=bag2,
                                     item="农残", method="GC-MS", standard="国标", result="pass")
        self._release("rl-t4", "component", bag2)
        self.batch.issue_material(request_id="i-t4", actor_id="wh1", order_id="WO-2",
                                  step_id=t3, lot_id="LOT-PACK", quantity=1)
        out2 = self.batch.report_output(request_id="o-t5", actor_id="f1", order_id="WO-2",
                                        step_id=t3, good_quantity=1,
                                        outputs=[{"serial_no": "U-C1"}],
                                        inputs=[{"component_id": bag2, "quantity": 4.5}])
        self.batch.record_inspection(request_id="ci-t5", actor_id="qc1",
                                     target_type="finished_batch", target_id=out2["output_id"],
                                     item="标签", method="全检", standard="企标", result="pass")
        self._release("rl-t5", "finished_batch", out2["output_id"])
        self.batch.create_sales_order(request_id="so-t2", actor_id="b1", sales_order_id="SO-T2",
                                      channel="买手店", serials=["U-C1"])
        self.batch.ship_order(request_id="sh-t2", actor_id="b1", sales_order_id="SO-T2",
                              batch_id="CB-T2")

    def test_freeze_only_touches_affected_and_lift_restores(self):
        self._build_two_orders()
        freeze = self.batch.create_freeze(request_id="fz1", actor_id="qc1",
                                          target_type="material_lot", target_id="LOT-ALT",
                                          reason="替代料污染")
        frozen = {(item["target_type"], item["target_id"]) for item in freeze["frozen"]}
        self.assertIn(("material_lot", "LOT-ALT"), frozen)
        self.assertIn(("finished_unit", self._unit_id("U-T1")), frozen)
        self.assertIn(("finished_unit", self._unit_id("U-T2")), frozen)
        self.assertIn(("sales_order", "SO-T1"), frozen)
        self.assertIn(("channel_batch", "CB-T1"), frozen)
        # 已耗尽组件没有可冻结库存，但完整影响面可由召回接口查到。
        self.assertNotIn(("component", self._component_id("BLEND-T1")), frozen)
        recall = self.batch.recall_scope(actor_id="au1", target_type="material_lot",
                                         target_id="LOT-ALT")
        affected_components = {item["component_id"] for item in recall["affected"]["components"]}
        self.assertIn(self._component_id("BLEND-T1"), affected_components)
        # 未受影响的库存、订单与渠道批次保持原状。
        self.assertNotIn(("sales_order", "SO-T2"), frozen)
        self.assertNotIn(("channel_batch", "CB-T2"), frozen)
        self.assertEqual("available",
                         self.batch.get_lot(actor_id="au1", lot_id="LOT-TEA")["status"])
        self.assertEqual("shipped",
                         self.batch.get_unit(actor_id="au1", serial_no="U-C1")["status"])
        self.assertEqual("frozen",
                         self.batch.get_unit(actor_id="au1", serial_no="U-T1")["status"])
        self.assertEqual("frozen",
                         self.batch.get_unit(actor_id="au1", serial_no="U-T2")["status"])
        # 解除冻结后恢复冻结前状态。
        self.batch.lift_freeze(request_id="fz1-lift", actor_id="qc1",
                               freeze_id=freeze["freeze_id"])
        self.assertEqual("available",
                         self.batch.get_lot(actor_id="au1", lot_id="LOT-ALT")["status"])
        self.assertEqual("shipped",
                         self.batch.get_unit(actor_id="au1", serial_no="U-T1")["status"])

    def test_trace_backward_from_finished_unit(self):
        self._build_two_orders()
        trace = self.batch.trace_backward(actor_id="au1", target_type="finished_unit",
                                          target_id=self._unit_id("U-T1"))
        lots, substitutes, certificates = set(), [], []

        def walk(node):
            if node.get("type") == "material_lot":
                lots.add(node["id"])
                certificates.extend(node["certificates"])
                self.assertTrue(node["inspections"])
            for parent in node.get("parents", []):
                if parent.get("via", {}).get("relation") == "substitute":
                    substitutes.append(parent["via"])
                walk(parent["node"])

        walk(trace)
        self.assertEqual({"LOT-TEA", "LOT-ALT", "LOT-PACK"}, lots)
        self.assertEqual(1, len(substitutes))
        self.assertEqual("TEA", substitutes[0]["substituted_for"])
        self.assertTrue(certificates)

    def test_recall_scope_quantities(self):
        self._build_two_orders()
        recall = self.batch.recall_scope(actor_id="au1", target_type="material_lot",
                                         target_id="LOT-ALT")
        self.assertEqual(2, recall["quantities"]["units_total"])
        self.assertEqual(0, recall["quantities"]["units_difference"])
        self.assertTrue(recall["quantities"]["lots_balanced"])
        self.assertTrue(recall["quantities"]["steps_balanced"])
        order_ids = {order["sales_order_id"] for order in recall["affected"]["sales_orders"]}
        self.assertEqual({"SO-T1"}, order_ids)
        batch_ids = {batch["batch_id"] for batch in recall["affected"]["channel_batches"]}
        self.assertEqual({"CB-T1"}, batch_ids)
        lot_row = recall["affected"]["material_lots"][0]
        self.assertEqual(0, lot_row["difference"])

    def test_label_error_freeze_targets_only_that_combination(self):
        self._build_two_orders()
        output_id = self.database.connection.execute(
            "SELECT output_id FROM finished_units WHERE serial_no='U-C1'").fetchone()["output_id"]
        freeze = self.batch.create_freeze(request_id="fz-label", actor_id="qc1",
                                          target_type="finished_batch", target_id=output_id,
                                          reason="标签错误")
        frozen = {(item["target_type"], item["target_id"]) for item in freeze["frozen"]}
        self.assertIn(("finished_unit", self._unit_id("U-C1")), frozen)
        self.assertIn(("sales_order", "SO-T2"), frozen)
        self.assertIn(("channel_batch", "CB-T2"), frozen)
        self.assertNotIn(("material_lot", "LOT-TEA"), frozen)
        self.assertNotIn(("sales_order", "SO-T1"), frozen)


class PermissionTest(BatchControlTestBase):
    def test_role_boundaries(self):
        s1, _, _ = self._order()
        with self.assertRaises(PermissionDenied):
            self.batch.register_material_lot(
                request_id="p1", actor_id="qc1", site_id="s1", lot_id="LOT-X",
                material_code="TEA", material_name="茶", origin="x", supplier_name="s",
                quantity=1, unit="kg",
                certificates=[{"cert_type": "t", "cert_number": "n", "issuer": "i",
                               "issued_on": "d", "file_hash": "h"}])
        with self.assertRaises(PermissionDenied):
            self.batch.record_inspection(request_id="p2", actor_id="wh1",
                                         target_type="material_lot", target_id="LOT-TEA",
                                         item="x", method="m", standard="s", result="pass")
        with self.assertRaises(PermissionDenied):
            self.batch.issue_material(request_id="p3", actor_id="b1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-TEA", quantity=1)
        with self.assertRaises(PermissionDenied):
            self.batch.report_output(request_id="p4", actor_id="wh1", order_id="WO-1",
                                     step_id=s1, good_quantity=0)
        with self.assertRaises(PermissionDenied):
            self.batch.create_sales_order(request_id="p5", actor_id="f1",
                                          sales_order_id="SO-X", channel="c", serials=["U-1"])
        with self.assertRaises(PermissionDenied):
            self.batch.create_freeze(request_id="p6", actor_id="b1",
                                     target_type="material_lot", target_id="LOT-TEA", reason="r")
        with self.assertRaises(PermissionDenied):
            self.batch.create_recipe(request_id="p7", actor_id="f1", product_code="PROD-X",
                                     version=1, lines=[{"material_code": "TEA", "quantity": 1,
                                                        "unit": "kg"}])
        # 审计角色只读。
        with self.assertRaises(PermissionDenied):
            self.batch.issue_material(request_id="p8", actor_id="au1", order_id="WO-1",
                                      step_id=s1, lot_id="LOT-TEA", quantity=1)
        state = self.batch.verify_conservation(actor_id="au1", order_id="WO-1")
        self.assertTrue(state["balanced"])

    def test_unknown_actor_and_objects(self):
        with self.assertRaises(NotFoundError):
            self.batch.issue_material(request_id="n1", actor_id="ghost", order_id="WO-1",
                                      step_id="WO-1-S1", lot_id="LOT-TEA", quantity=1)
        self._order()
        with self.assertRaises(NotFoundError):
            self.batch.issue_material(request_id="n2", actor_id="wh1", order_id="WO-1",
                                      step_id="WO-1-S1", lot_id="LOT-VOID", quantity=1)


class ApiRouteTest(BatchControlTestBase):
    def test_batch_endpoints_and_fallback(self):
        from batch_control.api import make_route, route_batch

        route = make_route(self.domain, self.batch)
        # 基础服务路由仍然可用。
        status, payload = route("GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        # 批次接口：登记原料批次（201），同一 request_id 重放（200）。
        body = {"request_id": "api-lot", "site_id": "s1", "lot_id": "LOT-API",
                "material_code": "TEA", "material_name": "龙井", "origin": "杭州",
                "supplier_name": "供应商", "quantity": 5, "unit": "kg",
                "certificates": [{"cert_type": "产地证明", "cert_number": "CO-1",
                                  "issuer": "协会", "issued_on": "2026-09-30",
                                  "file_hash": "h1"}]}
        status, payload = route("POST", "/batch/material-lots", body, {"X-Actor-Id": "wh1"})
        self.assertEqual(201, status)
        self.assertEqual("LOT-API", payload["lot_id"])
        status, payload = route("POST", "/batch/material-lots", body, {"X-Actor-Id": "wh1"})
        self.assertEqual(200, status)
        # 角色错误 → 403。
        status, payload = route("POST", "/batch/material-lots",
                                {**body, "request_id": "api-lot-2", "lot_id": "LOT-API2"},
                                {"X-Actor-Id": "b1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])
        # 只读接口。
        status, payload = route("GET", "/batch/conservation?order_id=WO-1", None,
                                {"X-Actor-Id": "au1"})
        self.assertEqual(404, status)  # 工单不存在 → not_found
        self._order("WO-1")
        status, payload = route("GET", "/batch/conservation?order_id=WO-1", None,
                                {"X-Actor-Id": "au1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["balanced"])
        # 非本模块路径返回 None，由组合路由回退。
        self.assertIsNone(route_batch(self.batch, "GET", "/health", None))
        # 缺少必填字段 → 400。
        status, payload = route("POST", "/batch/issues", {"request_id": "x"},
                                {"X-Actor-Id": "wh1"})
        self.assertEqual(400, status)


class PersistenceTest(BatchControlTestBase):
    def test_restart_keeps_work_orders_and_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pilot.sqlite3"
            clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
            database = Database(path)
            domain = DomainService(database, clock)
            batch = BatchControlService(database, clock)
            domain.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="o1", name="机构")
            domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管理员", role="admin", organization_id="o1")
            for request_id, actor_id, role in (("wh", "wh1", "warehouse"), ("qc1", "qc1", "qc"),
                                               ("qc2", "qc2", "qc"), ("fac", "f1", "factory"),
                                               ("aud", "au1", "auditor")):
                domain.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                      display_name=actor_id, role=role, organization_id="o1")
            domain.register_site(request_id="site", actor_id="a1", site_id="s1",
                                 organization_id="o1", name="基地", timezone_name="Asia/Shanghai")
            recipe = batch.create_recipe(request_id="r1", actor_id="a1", product_code="GIFT-SET",
                                         version=1,
                                         lines=[{"material_code": "TEA", "quantity": 1,
                                                 "unit": "kg"}])
            batch.activate_recipe(request_id="r1a", actor_id="a1", recipe_id=recipe["recipe_id"])
            batch.register_material_lot(
                request_id="l1", actor_id="wh1", site_id="s1", lot_id="LOT-P",
                material_code="TEA", material_name="茶", origin="x", supplier_name="s",
                quantity=10, unit="kg",
                certificates=[{"cert_type": "t", "cert_number": "n", "issuer": "i",
                               "issued_on": "d", "file_hash": "h"}])
            batch.create_work_order(request_id="w1", actor_id="f1", order_id="WO-P", site_id="s1",
                                    product_code="GIFT-SET", planned_quantity=1, unit="套",
                                    steps=[{"name": "配料", "unit": "kg"},
                                           {"name": "包装", "unit": "套"}])
            batch.record_inspection(request_id="qi", actor_id="qc1",
                                    target_type="material_lot", target_id="LOT-P",
                                    item="x", method="m", standard="s", result="fail")
            batch.issue_material  # noqa: B018 - 仅确认服务可用
            database.close()

            database2 = Database(path)
            batch2 = BatchControlService(database2, clock)
            domain2 = DomainService(database2, clock)
            # 未完成工单与隔离状态在重启后继续有效。
            self.assertEqual("open", batch2.get_work_order(actor_id="au1",
                                                           order_id="WO-P")["status"])
            self.assertEqual("quarantined",
                             batch2.get_lot(actor_id="au1", lot_id="LOT-P")["status"])
            with self.assertRaises(ConflictError):
                batch2.issue_material(request_id="i-p", actor_id="wh1", order_id="WO-P",
                                      step_id="WO-P-S1", lot_id="LOT-P", quantity=1)
            valid, _ = domain2.verify_audit()
            self.assertTrue(valid)
            database2.close()


if __name__ == "__main__":
    unittest.main()

"""试产批次控制系统的领域服务测试。"""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import ConflictError, PermissionDenied, ValidationError
from creative_program_foundation.pilot.service import PilotService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class PilotCase(unittest.TestCase):
    """搭建一个四角色协作的最小可用环境。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "pilot-test.sqlite3")
        clock = FixedClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        self.ds = DomainService(self.database, clock)
        self.ps = PilotService(self.database, clock)
        self.ds.register_organization(request_id="org-req", actor_id="bootstrap",
                                      organization_id="o1", name="品牌机构")
        self.ds.register_actor(request_id="admin-req", actor_id="bootstrap", new_actor_id="adm",
                               display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in (
                ("wh-req", "wh", "仓库员", "warehouse"),
                ("qc-req", "qc", "质检员", "qinspector"),
                ("fac-req", "fac", "工厂员", "factory"),
                ("br-req", "br", "品牌方", "brand"),
                ("au-req", "au", "审计员", "auditor")):
            self.ds.register_actor(request_id=rid, actor_id="adm", new_actor_id=aid,
                                   display_name=name, role=role, organization_id="o1")
        self._master_data()

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def _master_data(self):
        ps = self.ps
        ps.register_supplier(request_id="sup", actor_id="wh", supplier_id="sup1", name="福建茶农")
        ps.register_material(request_id="mat-tea", actor_id="wh", material_id="tea",
                             kind="tea", name="大红袍", unit="kg")
        ps.register_material(request_id="mat-sub", actor_id="wh", material_id="teasub",
                             kind="tea", name="备用茶", unit="kg")
        ps.register_material(request_id="mat-glaze", actor_id="wh", material_id="glaze",
                             kind="glaze", name="天目釉", unit="kg")
        ps.register_material(request_id="mat-other-tea", actor_id="wh", material_id="tea2",
                             kind="tea", name="无关产品用茶", unit="kg")
        for rid, bid, mat, cert, origin in (
                ("bat1", "bat-tea", "tea", "CERT-1", "武夷山"),
                ("bat2", "bat-sub", "teasub", "CERT-2", "安溪"),
                ("bat3", "bat-glaze", "glaze", "CERT-3", "景德镇"),
                ("bat4", "bat-tea2", "tea2", "CERT-4", "杭州")):
            ps.register_material_batch(request_id=rid, actor_id="wh", batch_id=bid,
                                       material_id=mat, supplier_id="sup1", delivery_note="DN",
                                       cert_no=cert, cert_summary={"check": "ok"},
                                       origin_note=origin)
        self.lot_tea = ps.receive_lot(request_id="lot1", actor_id="wh", batch_id="bat-tea",
                                      label="TEA-LOT", qty=100).resource_id
        self.lot_sub = ps.receive_lot(request_id="lot2", actor_id="wh", batch_id="bat-sub",
                                      label="SUB-LOT", qty=50).resource_id
        self.lot_glaze = ps.receive_lot(request_id="lot3", actor_id="wh", batch_id="bat-glaze",
                                        label="GLAZE-LOT", qty=40).resource_id
        self.lot_other = ps.receive_lot(request_id="lot4", actor_id="wh", batch_id="bat-tea2",
                                        label="OTHER-LOT", qty=80).resource_id
        ps.register_inspection_spec(request_id="spec-tea", actor_id="qc", spec_id="sp-tea",
                                    applies_to="material", ref_id="tea", version=1,
                                    items=["农残", "含水率"])
        ps.register_inspection_spec(request_id="spec-sub", actor_id="qc", spec_id="sp-sub",
                                    applies_to="material", ref_id="teasub", version=1,
                                    items=["农残"])
        ps.register_inspection_spec(request_id="spec-glaze", actor_id="qc", spec_id="sp-glaze",
                                    applies_to="material", ref_id="glaze", version=1,
                                    items=["铅镉"])
        ps.register_inspection_spec(request_id="spec-tea2", actor_id="qc", spec_id="sp-tea2",
                                    applies_to="material", ref_id="tea2", version=1,
                                    items=["农残"])
        for rid, lot, spec in (("insp1", self.lot_tea, "sp-tea"),
                               ("insp2", self.lot_sub, "sp-sub"),
                               ("insp3", self.lot_glaze, "sp-glaze"),
                               ("insp4", self.lot_other, "sp-tea2")):
            ps.inspect_lot(request_id=rid, actor_id="qc", lot_id=lot, spec_id=spec,
                           decision="pass")
        # 两道工序：1 茶坯（茶叶可替代 + 釉），2 成器（上工序组件）
        ps.register_recipe(request_id="recipe", actor_id="br", recipe_id="rec1",
                           product_code="TEASET", version=1,
                           lines=[
                               {"line_seq": 1, "step_seq": 1, "input_kind": "material",
                                "input_ref_id": "tea", "qty_per": 1.0,
                                "allow_substitute": True},
                               {"line_seq": 2, "step_seq": 1, "input_kind": "material",
                                "input_ref_id": "glaze", "qty_per": 0.5,
                                "allow_substitute": False},
                           ])
        ps.register_inspection_spec(request_id="spec-comp", actor_id="qc", spec_id="sp-comp",
                                    applies_to="component", ref_id="TEASET", version=1,
                                    items=["外观"])
        ps.register_order(request_id="order", actor_id="br", order_id="ord1", channel="门店",
                          product_code="TEASET", qty=10)
        ps.register_channel_batch(request_id="chan", actor_id="br", batch_id="cb1",
                                  order_id="ord1", qty_planned=10)

    # ------------------------------------------------------------------
    # 辅助：完整生产一小批
    # ------------------------------------------------------------------

    def _produce(self, wo="wo1", planned=10, tea_qty=8.0, sub_qty=2.0,
                 good=8, scrap=1, rework=1, rework_mode="repaired"):
        ps = self.ps
        ps.open_work_order(request_id=f"open-{wo}", actor_id="fac", wo_id=wo,
                           recipe_id="rec1", planned_qty=planned)
        ps.issue_material(request_id=f"i-tea-{wo}", actor_id="wh", wo_id=wo, line_seq=1,
                          lot_id=self.lot_tea, qty=tea_qty, scan_ref=f"SCAN-T-{wo}")
        if sub_qty:
            ps.issue_material(request_id=f"i-sub-{wo}", actor_id="wh", wo_id=wo, line_seq=1,
                              lot_id=self.lot_sub, qty=sub_qty, scan_ref=f"SCAN-S-{wo}")
        ps.issue_material(request_id=f"i-gl-{wo}", actor_id="wh", wo_id=wo, line_seq=2,
                          lot_id=self.lot_glaze, qty=0.5 * (good + scrap + rework),
                          scan_ref=f"SCAN-G-{wo}")
        usage = [{"1": self.lot_tea, "2": self.lot_glaze}] * int(good)
        usage += [{"1": self.lot_sub, "2": self.lot_glaze}] * int(scrap + rework)
        out = ps.report_output(
            request_id=f"out1-{wo}", actor_id="fac", wo_id=wo, step_seq=1,
            qty_good=good, qty_scrap=scrap, qty_rework=rework,
            materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": tea_qty},
                       {"line_seq": 1, "lot_id": self.lot_sub, "qty": sub_qty},
                       {"line_seq": 2, "lot_id": self.lot_glaze,
                        "qty": 0.5 * (good + scrap + rework)}],
            material_usage=usage)
        import json
        row = self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id=?",
            (f"out1-{wo}",)).fetchone()
        resp = json.loads(row["response_json"])
        good_ids, rework_ids = resp["components"], resp["rework_components"]
        self._inspect_and_release(good_ids, wo)
        reworked_ids = []
        for rw in rework_ids:
            rwid = self.database.connection.execute(
                "SELECT rework_id FROM rework_jobs WHERE source_component_id=?",
                (rw,)).fetchone()["rework_id"]
            ps.finish_rework(request_id=f"rwf-{rw[:8]}-{wo}", actor_id="fac",
                             rework_id=rwid, decision=rework_mode)
            if rework_mode == "rebuilt":
                result_id = self.database.connection.execute(
                    "SELECT result_component_id FROM rework_jobs WHERE rework_id=?",
                    (rwid,)).fetchone()["result_component_id"]
            else:
                result_id = rw
            ps.inspect_component(request_id=f"ic-rw-{rw[:8]}-{wo}", actor_id="qc",
                                 component_id=result_id, spec_id="sp-comp", decision="pass")
            ticket = ps.request_release(request_id=f"rr-rw-{rw[:8]}-{wo}", actor_id="fac",
                                        component_id=result_id)
            ps.approve_release(request_id=f"arw-q-{rw[:8]}-{wo}", actor_id="qc",
                               ticket_id=ticket.resource_id, role="qinspector")
            ps.approve_release(request_id=f"arw-b-{rw[:8]}-{wo}", actor_id="br",
                               ticket_id=ticket.resource_id, role="brand")
            reworked_ids.append(result_id)
        return good_ids + reworked_ids

    def _inspect_and_release(self, component_ids, wo):
        ps = self.ps
        for index, cid in enumerate(component_ids):
            ps.inspect_component(request_id=f"ic-{wo}-{index}", actor_id="qc",
                                 component_id=cid, spec_id="sp-comp", decision="pass")
            ticket = ps.request_release(request_id=f"rr-{wo}-{index}", actor_id="fac",
                                        component_id=cid)
            ps.approve_release(request_id=f"ar-q-{wo}-{index}", actor_id="qc",
                               ticket_id=ticket.resource_id, role="qinspector")
            ps.approve_release(request_id=f"ar-b-{wo}-{index}", actor_id="br",
                               ticket_id=ticket.resource_id, role="brand")

    # ------------------------------------------------------------------
    # 登记与角色
    # ------------------------------------------------------------------

    def test_roles_are_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.ps.register_supplier(request_id="x1", actor_id="fac",
                                      supplier_id="zzz", name="无权")
        with self.assertRaises(PermissionDenied):
            self.ps.open_work_order(request_id="x2", actor_id="wh", wo_id="zzz",
                                    recipe_id="rec1", planned_qty=1)

    def test_uninspected_lot_cannot_be_issued(self):
        lot = self.ps.receive_lot(request_id="fresh", actor_id="wh", batch_id="bat-tea",
                                  label="FRESH", qty=10).resource_id
        self.ps.open_work_order(request_id="wopen", actor_id="fac", wo_id="wx",
                                recipe_id="rec1", planned_qty=1)
        with self.assertRaises(ConflictError):
            self.ps.issue_material(request_id="wx-i", actor_id="wh", wo_id="wx", line_seq=1,
                                   lot_id=lot, qty=1, scan_ref="S1")

    def test_substitute_requires_recipe_flag(self):
        self.ps.open_work_order(request_id="wopen2", actor_id="fac", wo_id="wy",
                                recipe_id="rec1", planned_qty=1)
        # 釉料行不允许替代：发茶叶批次到釉料行必须拒绝
        with self.assertRaises(ConflictError):
            self.ps.issue_material(request_id="wy-i", actor_id="wh", wo_id="wy", line_seq=2,
                                   lot_id=self.lot_tea, qty=0.5, scan_ref="S2")

    # ------------------------------------------------------------------
    # 扫码幂等 / 并发 / 负库存
    # ------------------------------------------------------------------

    def test_duplicate_scan_does_not_double_issue(self):
        self.ps.open_work_order(request_id="wd", actor_id="fac", wo_id="wd1",
                                recipe_id="rec1", planned_qty=5)
        kwargs = dict(actor_id="wh", wo_id="wd1", line_seq=1, lot_id=self.lot_tea,
                      qty=3, scan_ref="DUP-1")
        self.ps.issue_material(request_id="r-a", **kwargs)
        # 相同扫码、不同 request_id 也必须被拒绝
        with self.assertRaises(ConflictError):
            self.ps.issue_material(request_id="r-b", **kwargs)
        lot = self.ps.get_lot(self.lot_tea)
        self.assertEqual(97.0, lot["remaining_qty"])

    def test_idempotent_replay_keeps_single_effect(self):
        self.ps.open_work_order(request_id="we", actor_id="fac", wo_id="we1",
                                recipe_id="rec1", planned_qty=5)
        first = self.ps.issue_material(request_id="same-rid", actor_id="wh", wo_id="we1",
                                       line_seq=1, lot_id=self.lot_tea, qty=3, scan_ref="RP-1")
        second = self.ps.issue_material(request_id="same-rid", actor_id="wh", wo_id="we1",
                                        line_seq=1, lot_id=self.lot_tea, qty=3, scan_ref="RP-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(97.0, self.ps.get_lot(self.lot_tea)["remaining_qty"])

    def test_concurrent_issues_never_go_negative(self):
        # 库存仅 1，两个线程各领 1：只能一个成功，不能出现 -1
        tiny = self.ps.receive_lot(request_id="tiny", actor_id="wh", batch_id="bat-tea",
                                   label="TINY", qty=1).resource_id
        self.ps.inspect_lot(request_id="tiny-insp", actor_id="qc", lot_id=tiny,
                            spec_id="sp-tea", decision="pass")
        self.ps.open_work_order(request_id="wc", actor_id="fac", wo_id="wc1",
                                recipe_id="rec1", planned_qty=2)
        results = []

        def worker(rid, scan):
            try:
                self.ps.issue_material(request_id=rid, actor_id="wh", wo_id="wc1",
                                       line_seq=1, lot_id=tiny, qty=1, scan_ref=scan)
                results.append("ok")
            except ConflictError:
                results.append("blocked")

        threads = [threading.Thread(target=worker, args=("c1", "CC-1")),
                   threading.Thread(target=worker, args=("c2", "CC-2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["blocked", "ok"])
        self.assertEqual(0.0, self.ps.get_lot(tiny)["remaining_qty"])

    def test_over_issue_beyond_plan_rejected(self):
        self.ps.open_work_order(request_id="wf", actor_id="fac", wo_id="wf1",
                                recipe_id="rec1", planned_qty=2)
        with self.assertRaises(ConflictError):
            self.ps.issue_material(request_id="wf-i", actor_id="wh", wo_id="wf1",
                                   line_seq=1, lot_id=self.lot_tea, qty=3, scan_ref="OV-1")

    # ------------------------------------------------------------------
    # 守恒
    # ------------------------------------------------------------------

    def test_full_flow_quantities_balance(self):
        self._produce()
        balance = self.ps.verify_work_order_balance("wo1")
        self.assertTrue(balance["balanced"], balance)
        for lot in (self.lot_tea, self.lot_sub, self.lot_glaze):
            snap = self.ps.verify_lot_balance(lot)
            self.assertTrue(snap.balanced, snap)
            self.assertGreaterEqual(snap.wip_qty, -1e-9)

    def test_wip_scrap_balances_without_touching_stock(self):
        self.ps.open_work_order(request_id="wg", actor_id="fac", wo_id="wg1",
                                recipe_id="rec1", planned_qty=5)
        self.ps.issue_material(request_id="wg-i", actor_id="wh", wo_id="wg1", line_seq=1,
                               lot_id=self.lot_tea, qty=5, scan_ref="WG-1")
        stock_before = self.ps.get_lot(self.lot_tea)["remaining_qty"]
        self.ps.report_wip_scrap(request_id="wg-scrap", actor_id="fac", wo_id="wg1",
                                 lot_id=self.lot_tea, qty=2, note="撒漏")
        # 仓库库存不变
        self.assertEqual(stock_before, self.ps.get_lot(self.lot_tea)["remaining_qty"])
        # 不能报废超过在制量
        with self.assertRaises(ConflictError):
            self.ps.report_wip_scrap(request_id="wg-scrap2", actor_id="fac", wo_id="wg1",
                                     lot_id=self.lot_tea, qty=4, note="超量")
        snap = self.ps.verify_lot_balance(self.lot_tea)
        self.assertEqual(5.0, snap.input_qty)
        self.assertEqual(2.0, snap.scrap_qty)
        self.assertEqual(3.0, snap.wip_qty)

    def test_return_cannot_exceed_wip(self):
        self.ps.open_work_order(request_id="wh2", actor_id="fac", wo_id="wh1",
                                recipe_id="rec1", planned_qty=3)
        self.ps.issue_material(request_id="wh-i", actor_id="wh", wo_id="wh1", line_seq=1,
                               lot_id=self.lot_tea, qty=3, scan_ref="RT-1")
        with self.assertRaises(ConflictError):
            self.ps.return_material(request_id="wh-r", actor_id="wh", wo_id="wh1",
                                    line_seq=1, lot_id=self.lot_tea, qty=4, scan_ref="RT-2")
        self.ps.return_material(request_id="wh-r2", actor_id="wh", wo_id="wh1",
                                line_seq=1, lot_id=self.lot_tea, qty=1, scan_ref="RT-3")
        self.assertEqual(98.0, self.ps.get_lot(self.lot_tea)["remaining_qty"])

    def test_missing_material_line_rejected(self):
        self.ps.open_work_order(request_id="wi", actor_id="fac", wo_id="wi1",
                                recipe_id="rec1", planned_qty=2)
        self.ps.issue_material(request_id="wi-i1", actor_id="wh", wo_id="wi1", line_seq=1,
                               lot_id=self.lot_tea, qty=2, scan_ref="MI-1")
        self.ps.issue_material(request_id="wi-i2", actor_id="wh", wo_id="wi1", line_seq=2,
                               lot_id=self.lot_glaze, qty=1, scan_ref="MI-2")
        # 釉料行核销不足（应投 1，投 0.5）
        with self.assertRaises(ConflictError):
            self.ps.report_output(
                request_id="wi-o", actor_id="fac", wo_id="wi1", step_seq=1,
                qty_good=2, qty_scrap=0, qty_rework=0,
                materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": 2},
                           {"line_seq": 2, "lot_id": self.lot_glaze, "qty": 0.5}],
                material_usage=[{"1": self.lot_tea, "2": self.lot_glaze},
                                {"1": self.lot_tea, "2": self.lot_glaze}])

    def test_multi_lot_requires_per_unit_usage(self):
        self.ps.open_work_order(request_id="wj", actor_id="fac", wo_id="wj1",
                                recipe_id="rec1", planned_qty=2)
        self.ps.issue_material(request_id="wj-i1", actor_id="wh", wo_id="wj1", line_seq=1,
                               lot_id=self.lot_tea, qty=1, scan_ref="MJ-1")
        self.ps.issue_material(request_id="wj-i2", actor_id="wh", wo_id="wj1", line_seq=1,
                               lot_id=self.lot_sub, qty=1, scan_ref="MJ-2")
        self.ps.issue_material(request_id="wj-i3", actor_id="wh", wo_id="wj1", line_seq=2,
                               lot_id=self.lot_glaze, qty=1, scan_ref="MJ-3")
        with self.assertRaises(ConflictError):
            self.ps.report_output(
                request_id="wj-o", actor_id="fac", wo_id="wj1", step_seq=1,
                qty_good=2, qty_scrap=0, qty_rework=0,
                materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": 1},
                           {"line_seq": 1, "lot_id": self.lot_sub, "qty": 1},
                           {"line_seq": 2, "lot_id": self.lot_glaze, "qty": 1}])

    # ------------------------------------------------------------------
    # 放行闸门
    # ------------------------------------------------------------------

    def test_component_needs_inspection_and_dual_release(self):
        self.ps.open_work_order(request_id="wk", actor_id="fac", wo_id="wk1",
                                recipe_id="rec1", planned_qty=1)
        self.ps.issue_material(request_id="wk-i1", actor_id="wh", wo_id="wk1", line_seq=1,
                               lot_id=self.lot_tea, qty=1, scan_ref="MK-1")
        self.ps.issue_material(request_id="wk-i2", actor_id="wh", wo_id="wk1", line_seq=2,
                               lot_id=self.lot_glaze, qty=0.5, scan_ref="MK-2")
        import json
        self.ps.report_output(
            request_id="wk-o", actor_id="fac", wo_id="wk1", step_seq=1,
            qty_good=1, qty_scrap=0, qty_rework=0,
            materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": 1},
                       {"line_seq": 2, "lot_id": self.lot_glaze, "qty": 0.5}],
            material_usage=[{"1": self.lot_tea, "2": self.lot_glaze}])
        cid = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='wk-o'").fetchone()[0]
        )["components"][0]
        # 未检验不能申请放行
        with self.assertRaises(ConflictError):
            self.ps.request_release(request_id="wk-r0", actor_id="fac", component_id=cid)
        self.ps.inspect_component(request_id="wk-ic", actor_id="qc", component_id=cid,
                                  spec_id="sp-comp", decision="pass")
        ticket = self.ps.request_release(request_id="wk-rr", actor_id="fac",
                                         component_id=cid).resource_id
        # 单人批准后仍不能流转
        self.ps.approve_release(request_id="wk-a1", actor_id="qc", ticket_id=ticket,
                                role="qinspector")
        self.assertEqual("pending_inspection", self.ps.get_component(cid)["status"])
        # 同一人不能充当两个角色
        with self.assertRaises((ConflictError, PermissionDenied)):
            self.ps.approve_release(request_id="wk-a2bad", actor_id="qc", ticket_id=ticket,
                                    role="brand")
        # 第二人批准后放行
        self.ps.approve_release(request_id="wk-a2", actor_id="br", ticket_id=ticket,
                                role="brand")
        self.assertEqual("released", self.ps.get_component(cid)["status"])

    def test_failed_inspection_blocks_component(self):
        import json
        self._produce("wl", planned=2, tea_qty=2, sub_qty=0, good=2, scrap=0, rework=0) \
            if False else None
        self.ps.open_work_order(request_id="wl-open", actor_id="fac", wo_id="wl1",
                                recipe_id="rec1", planned_qty=1)
        self.ps.issue_material(request_id="wl-i1", actor_id="wh", wo_id="wl1", line_seq=1,
                               lot_id=self.lot_tea, qty=1, scan_ref="ML-1")
        self.ps.issue_material(request_id="wl-i2", actor_id="wh", wo_id="wl1", line_seq=2,
                               lot_id=self.lot_glaze, qty=0.5, scan_ref="ML-2")
        self.ps.report_output(
            request_id="wl-o", actor_id="fac", wo_id="wl1", step_seq=1,
            qty_good=1, qty_scrap=0, qty_rework=0,
            materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": 1},
                       {"line_seq": 2, "lot_id": self.lot_glaze, "qty": 0.5}],
            material_usage=[{"1": self.lot_tea, "2": self.lot_glaze}])
        cid = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='wl-o'").fetchone()[0]
        )["components"][0]
        self.ps.inspect_component(request_id="wl-fail", actor_id="qc", component_id=cid,
                                  spec_id="sp-comp", decision="fail", findings={"defect": "裂"})
        self.assertEqual("blocked", self.ps.get_component(cid)["status"])
        # 可立案返工并修复
        rw = self.ps.open_rework(request_id="wl-rwopen", actor_id="fac", component_id=cid,
                                 note="裂纹返工").resource_id
        self.ps.finish_rework(request_id="wl-rwfin", actor_id="fac", rework_id=rw,
                              decision="repaired")
        self.assertEqual("pending_inspection", self.ps.get_component(cid)["status"])

    def test_unreleased_component_cannot_enter_next_step(self):
        import json
        self.ps.open_work_order(request_id="wm", actor_id="fac", wo_id="wm1",
                                recipe_id="rec1", planned_qty=1)
        self.ps.issue_material(request_id="wm-i1", actor_id="wh", wo_id="wm1", line_seq=1,
                               lot_id=self.lot_tea, qty=1, scan_ref="MM-1")
        self.ps.issue_material(request_id="wm-i2", actor_id="wh", wo_id="wm1", line_seq=2,
                               lot_id=self.lot_glaze, qty=0.5, scan_ref="MM-2")
        self.ps.report_output(
            request_id="wm-o1", actor_id="fac", wo_id="wm1", step_seq=1,
            qty_good=1, qty_scrap=0, qty_rework=0,
            materials=[{"line_seq": 1, "lot_id": self.lot_tea, "qty": 1},
                       {"line_seq": 2, "lot_id": self.lot_glaze, "qty": 0.5}],
            material_usage=[{"1": self.lot_tea, "2": self.lot_glaze}])
        cid = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='wm-o1'").fetchone()[0]
        )["components"][0]
        with self.assertRaises(ConflictError):
            self.ps.report_output(request_id="wm-o2", actor_id="fac", wo_id="wm1",
                                  step_seq=2, qty_good=1, qty_scrap=0, qty_rework=0,
                                  input_components=[cid])

    # ------------------------------------------------------------------
    # 拆包重组 / 标签
    # ------------------------------------------------------------------

    def test_repack_preserves_lineage_and_balance(self):
        child = self.ps.repack_lot(
            request_id="rp1", actor_id="wh",
            inputs=[{"lot_id": self.lot_tea, "qty": 10}],
            outputs=[{"label": "TEA-A", "qty": 6}, {"label": "TEA-B", "qty": 4}],
            note="分装")
        self.assertFalse(child.replayed)
        self.assertEqual(90.0, self.ps.get_lot(self.lot_tea)["remaining_qty"])
        # 不守恒的重组被拒绝
        with self.assertRaises(ValidationError):
            self.ps.repack_lot(
                request_id="rp2", actor_id="wh",
                inputs=[{"lot_id": self.lot_tea, "qty": 5}],
                outputs=[{"label": "BAD", "qty": 6}], note="多出来")
        # 不同物料不能合并
        with self.assertRaises(ValidationError):
            self.ps.repack_lot(
                request_id="rp3", actor_id="wh",
                inputs=[{"lot_id": self.lot_tea, "qty": 1},
                        {"lot_id": self.lot_glaze, "qty": 1}],
                outputs=[{"label": "MIX", "qty": 2}], note="混料")

    def test_relabel_keeps_history(self):
        self.ps.relabel_lot(request_id="rl1", actor_id="qc", lot_id=self.lot_tea,
                            new_label="CORRECTED", reason="原标签产地错误")
        self.assertEqual("CORRECTED", self.ps.get_lot(self.lot_tea)["label"])
        rows = self.database.connection.execute(
            "SELECT old_label,new_label FROM lot_relabels WHERE lot_id=?",
            (self.lot_tea,)).fetchall()
        self.assertEqual("TEA-LOT", rows[0]["old_label"])

    # ------------------------------------------------------------------
    # 精准冻结 / 召回
    # ------------------------------------------------------------------

    def test_freeze_from_substitute_lot_only_hits_real_descendants(self):
        # 8 件好茶 + 报废/返工各 1 件用替代料；最终 8 件好茶进包装
        step1 = self._produce()
        self.assertEqual(9, len(step1))
        out = self.ps.report_output(request_id="final-out", actor_id="fac", wo_id="wo1",
                                    step_seq=2, qty_good=8, qty_scrap=1, qty_rework=0,
                                    input_components=step1)
        import json
        final = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='final-out'").fetchone()[0]
        )["components"]
        self._inspect_and_release(final, "final")
        self.ps.pack_package(request_id="pack1", actor_id="br", channel_batch_id="cb1",
                             component_ids=final)
        # 从替代料批次发起污染冻结
        event = self.ps.raise_freeze(request_id="fz1", actor_id="qc", source_kind="lot",
                                     source_id=self.lot_sub, reason="contamination",
                                     note="农残超标")
        self.assertFalse(event.replayed)
        # 受影响组件：报废件 + 返工件(step1) + 其 step2 报废件 = 3；不含 8 件好茶
        recall = self.ps.recall_scope(source_kind="lot", source_id=self.lot_sub)
        self.assertEqual(3, len(recall.components))
        self.assertEqual(0, len(recall.packages))
        self.assertEqual([], recall.channel_batches)
        self.assertEqual([], recall.orders)
        # 好茶包装仍可销售，订单未冻结
        self.assertEqual("open", self.database.connection.execute(
            "SELECT status FROM orders WHERE order_id='ord1'").fetchone()[0])
        # 问题批次及其下游被冻结
        self.assertEqual("frozen", self.ps.get_lot(self.lot_sub)["status"])
        for cid in recall.components:
            self.assertTrue(self.ps.get_component(cid)["frozen"])

    def test_freeze_from_main_lot_covers_packages_and_channel(self):
        step1 = self._produce()
        import json
        self.ps.report_output(request_id="final-out2", actor_id="fac", wo_id="wo1",
                              step_seq=2, qty_good=8, qty_scrap=1, qty_rework=0,
                              input_components=step1)
        final = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='final-out2'").fetchone()[0]
        )["components"]
        self._inspect_and_release(final, "final2")
        self.ps.pack_package(request_id="pack2", actor_id="br", channel_batch_id="cb1",
                             component_ids=final)
        self.ps.raise_freeze(request_id="fz2", actor_id="br", source_kind="lot",
                             source_id=self.lot_tea, reason="mislabel", note="标签产地不符")
        recall = self.ps.recall_scope(source_kind="lot", source_id=self.lot_tea)
        # 8 件主料成品全部在召回范围，包装/渠道/订单受影响
        final_set = set(final)
        self.assertTrue(final_set <= set(recall.components))
        self.assertEqual(1, len(recall.packages))
        self.assertEqual(["cb1"], recall.channel_batches)
        self.assertEqual(["ord1"], recall.orders)
        self.assertEqual("frozen", self.database.connection.execute(
            "SELECT status FROM channel_batches WHERE batch_id='cb1'").fetchone()[0])

    def test_unrelated_product_lot_is_not_affected(self):
        self._produce()
        # 无关产品的检验合格批次从未被任何工单使用
        recall = self.ps.recall_scope(source_kind="lot", source_id=self.lot_tea)
        self.assertNotIn(self.lot_other, recall.lots)
        # 反向：从无关批次出发，下游为空
        other_recall = self.ps.recall_scope(source_kind="lot", source_id=self.lot_other)
        self.assertEqual([], other_recall.components)
        self.assertEqual([], other_recall.packages)

    def test_additional_inspection_fail_triggers_targeted_freeze(self):
        step1 = self._produce()
        # 已放行成品追加抽检不合格 → 只冻结该组件及其真实下游
        target = step1[0]
        self.ps.inspect_component(request_id="add-fail", actor_id="qc", component_id=target,
                                  spec_id="sp-comp", decision="fail",
                                  is_additional=True, findings={"new": "异味"})
        events = self.ps.list_freeze_events(active_only=True)
        self.assertTrue(any(e["source_id"] == target for e in events))

    def test_lift_freeze_restores_status(self):
        self.ps.raise_freeze(request_id="fz3", actor_id="qc", source_kind="lot",
                             source_id=self.lot_glaze, reason="contamination", note="x")
        self.assertEqual("frozen", self.ps.get_lot(self.lot_glaze)["status"])
        event = self.ps.list_freeze_events(active_only=True)[0]["event_id"]
        self.ps.lift_freeze(request_id="lift1", actor_id="qc", event_id=event)
        self.assertEqual("available", self.ps.get_lot(self.lot_glaze)["status"])

    # ------------------------------------------------------------------
    # 追溯
    # ------------------------------------------------------------------

    def test_trace_finished_good_back_to_sources(self):
        import json
        step1 = self._produce()
        self.ps.report_output(request_id="tr-out", actor_id="fac", wo_id="wo1", step_seq=2,
                              qty_good=9, qty_scrap=0, qty_rework=0,
                              input_components=step1)
        final = json.loads(self.database.connection.execute(
            "SELECT response_json FROM pilot_receipts WHERE request_id='tr-out'").fetchone()[0]
        )["components"]
        trace = self.ps.trace_component(final[0])
        lot_ids = {n["entity_id"] for n in trace["nodes"] if n["entity_type"] == "lot"}
        # 好茶只用主茶叶 + 釉，不含替代料
        self.assertIn(self.lot_tea, lot_ids)
        self.assertIn(self.lot_glaze, lot_ids)
        self.assertNotIn(self.lot_sub, lot_ids)
        # 供应证明可从谱系节点直接读出
        certs = {n["detail"].get("cert_no") for n in trace["nodes"]
                 if n["entity_type"] == "lot"}
        self.assertIn("CERT-1", certs)

    def test_rebuilt_component_preserves_original_lineage(self):
        import json
        # 产出 1 合格 + 1 返工，返工选择 rebuilt
        step1 = self._produce("rb1", planned=2, tea_qty=1, sub_qty=1,
                              good=1, scrap=0, rework=1, rework_mode="rebuilt")
        # 返工重建件是新组件
        job = self.database.connection.execute(
            "SELECT source_component_id, result_component_id FROM rework_jobs WHERE wo_id='rb1'"
        ).fetchone()
        self.assertNotEqual(job["result_component_id"], job["source_component_id"])
        rebuilt = job["result_component_id"]
        # 源件状态为 reworked，新件才是放行对象
        self.assertEqual("reworked", self.ps.get_component(job["source_component_id"])["status"])
        self.assertEqual("released", self.ps.get_component(rebuilt)["status"])
        # 新组件保留 reworked_from 与原始用料
        trace = self.ps.trace_component(rebuilt)
        relations = {n["relation"] for n in trace["nodes"]}
        self.assertIn("reworked_from", relations)
        lot_ids = {n["entity_id"] for n in trace["nodes"] if n["entity_type"] == "lot"}
        self.assertIn(self.lot_sub, lot_ids)

    def test_close_work_order_requires_zero_wip(self):
        # 完整生产 10 件排产量，全部物料消耗、返工结案后可以关单
        self._produce()
        receipt = self.ps.close_work_order(request_id="close-ok", actor_id="fac", wo_id="wo1")
        self.assertFalse(receipt.replayed)
        self.assertEqual("closed", self.ps.get_work_order("wo1")["status"])
        # 结案后不能再领料
        with self.assertRaises(ConflictError):
            self.ps.issue_material(request_id="after-close", actor_id="wh", wo_id="wo1",
                                   line_seq=1, lot_id=self.lot_tea, qty=1, scan_ref="AC-1")

    def test_close_blocked_when_wip_remains(self):
        self.ps.open_work_order(request_id="cw", actor_id="fac", wo_id="cw1",
                                recipe_id="rec1", planned_qty=5)
        self.ps.issue_material(request_id="cw-i", actor_id="wh", wo_id="cw1", line_seq=1,
                               lot_id=self.lot_tea, qty=5, scan_ref="CW-1")
        with self.assertRaises(ConflictError):
            self.ps.close_work_order(request_id="cw-close", actor_id="fac", wo_id="cw1")

    # ------------------------------------------------------------------
    # 持久化：重启后状态继续有效
    # ------------------------------------------------------------------

    def test_state_survives_restart(self):
        self._produce()
        # 冻结状态也必须随持久化保留
        self.ps.raise_freeze(request_id="fz-restart", actor_id="qc", source_kind="lot",
                             source_id=self.lot_glaze, reason="contamination", note="待查")
        path = self.database.path
        self.database.close()
        database = Database(path)
        ps = PilotService(database)
        try:
            balance = ps.verify_work_order_balance("wo1")
            self.assertTrue(balance["balanced"])
            # 未完工工单仍是 in_progress，检验合格批次仍可用
            self.assertEqual("in_progress", ps.get_work_order("wo1")["status"])
            self.assertEqual("available", ps.get_lot(self.lot_tea)["status"])
            # 冻结事件与隔离状态重启后继续有效
            self.assertEqual(1, len(ps.list_freeze_events(active_only=True)))
            self.assertEqual("frozen", ps.get_lot(self.lot_glaze)["status"])
        finally:
            database.close()

    def test_audit_chain_covers_pilot_actions(self):
        self._produce()
        valid, count = self.ds.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()

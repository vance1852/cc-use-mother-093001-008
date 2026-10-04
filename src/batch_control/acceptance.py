"""运行茶·道试产批次控制的离线端到端验收。

在临时 SQLite 数据库中走通：登记原料批次与供应证明、检验、配方版本、
工单、幂等领料、替代料、工序产出守恒、双人放行、追加抽检、包装组合、
销售与渠道发货、污染定向冻结、反向追溯、正向召回与重启恢复，
成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

from .service import BatchControlService


def _setup(domain: DomainService) -> None:
    domain.register_organization(request_id="acc-org", actor_id="bootstrap",
                                 organization_id="org-tea", name="茶道文创转化中心")
    domain.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-1",
                          display_name="系统管理员", role="admin", organization_id="org-tea")
    for request_id, actor_id, name, role in (
            ("acc-wh", "wh-1", "仓库主管", "warehouse"),
            ("acc-qc1", "qc-1", "质检甲", "qc"),
            ("acc-qc2", "qc-2", "质检乙", "qc"),
            ("acc-factory", "factory-1", "组装厂计划员", "factory"),
            ("acc-brand", "brand-1", "品牌方运营", "brand"),
            ("acc-auditor", "auditor-1", "审计员", "auditor")):
        domain.register_actor(request_id=request_id, actor_id="admin-1", new_actor_id=actor_id,
                              display_name=name, role=role, organization_id="org-tea")
    domain.register_site(request_id="acc-site", actor_id="admin-1", site_id="site-pilot",
                         organization_id="org-tea", name="试产基地", timezone_name="Asia/Shanghai")


def _register_lot(batch: BatchControlService, request_id: str, lot_id: str, code: str,
                  name: str, origin: str, quantity: float, unit: str) -> None:
    batch.register_material_lot(
        request_id=request_id, actor_id="wh-1", site_id="site-pilot", lot_id=lot_id,
        material_code=code, material_name=name, origin=origin, supplier_name="合作供应商",
        quantity=quantity, unit=unit,
        certificates=[{"cert_type": "产地证明", "cert_number": f"CO-{lot_id}",
                       "issuer": "产地行业协会", "issued_on": "2026-09-20",
                       "file_hash": f"hash-{lot_id}-co"},
                      {"cert_type": "出厂检验报告", "cert_number": f"QC-{lot_id}",
                       "issuer": "供应商实验室", "issued_on": "2026-09-21",
                       "file_hash": f"hash-{lot_id}-qc"}])
    batch.record_inspection(request_id=f"{request_id}-insp", actor_id="qc-1",
                            target_type="material_lot", target_id=lot_id,
                            item="感官与理化指标", method="按批抽检", standard="企业标准 Q/TD 001",
                            result="pass", required=True)


def _release(batch: BatchControlService, request_prefix: str, target_type: str,
             target_id: str) -> None:
    first = batch.approve_release(request_id=f"{request_prefix}-r1", actor_id="qc-1",
                                  target_type=target_type, target_id=target_id)
    assert first["status"] == "pending_first", first
    second = batch.approve_release(request_id=f"{request_prefix}-r2", actor_id="qc-2",
                                   target_type=target_type, target_id=target_id)
    assert second["status"] == "released", second


def _component_id(batch: BatchControlService, serial: str) -> str:
    row = batch.database.connection.execute(
        "SELECT component_id FROM components WHERE serial_no=?", (serial,)).fetchone()
    assert row is not None, serial
    return row["component_id"]


def _unit_id(batch: BatchControlService, serial: str) -> str:
    row = batch.database.connection.execute(
        "SELECT unit_id FROM finished_units WHERE serial_no=?", (serial,)).fetchone()
    assert row is not None, serial
    return row["unit_id"]


def _produce_order(batch: BatchControlService, prefix: str, order_id: str,
                   unit_serials: list[str], tea_lot: str, use_substitute: bool) -> str:
    """走完一个工单从领料到包装放行的全流程，返回包装产出编号。"""

    batch.create_work_order(request_id=f"{prefix}-wo", actor_id="factory-1", order_id=order_id,
                            site_id="site-pilot", product_code="GIFT-SET",
                            planned_quantity=len(unit_serials), unit="套",
                            steps=[{"name": "配料", "unit": "kg"},
                                   {"name": "分装", "unit": "kg"},
                                   {"name": "制杯", "unit": "kg"},
                                   {"name": "包装", "unit": "套"}])
    blend_qty = round(len(unit_serials) * 0.25 + 2.5, 6)
    batch.issue_material(request_id=f"{prefix}-i1", actor_id="wh-1", order_id=order_id,
                         step_id=f"{order_id}-S1", lot_id=tea_lot, quantity=blend_qty - 2)
    if use_substitute:
        batch.issue_material(request_id=f"{prefix}-i2", actor_id="wh-1", order_id=order_id,
                             step_id=f"{order_id}-S1", lot_id="LOT-ALT", quantity=2,
                             substituted_for="TEA")
    else:
        batch.issue_material(request_id=f"{prefix}-i2", actor_id="wh-1", order_id=order_id,
                             step_id=f"{order_id}-S1", lot_id=tea_lot, quantity=2)
    blend_serial = f"BLEND-{order_id}"
    batch.report_output(request_id=f"{prefix}-o1", actor_id="factory-1", order_id=order_id,
                        step_id=f"{order_id}-S1", good_quantity=blend_qty - 0.5,
                        scrap_quantity=0.5,
                        outputs=[{"serial_no": blend_serial, "component_code": "MIX-TEA",
                                  "quantity": blend_qty - 0.5}])
    blend_id = _component_id(batch, blend_serial)
    batch.record_inspection(request_id=f"{prefix}-ci1", actor_id="qc-1",
                            target_type="component", target_id=blend_id,
                            item="混合均匀度", method="抽样", standard="Q/TD 002", result="pass")
    _release(batch, f"{prefix}-b1", "component", blend_id)
    bag_qty = round(blend_qty - 1.0, 6)
    bag_serial = f"TB-{order_id}"
    batch.report_output(request_id=f"{prefix}-o2", actor_id="factory-1", order_id=order_id,
                        step_id=f"{order_id}-S2", good_quantity=bag_qty, scrap_quantity=0.5,
                        outputs=[{"serial_no": bag_serial, "component_code": "TEA-BAG",
                                  "quantity": bag_qty}],
                        inputs=[{"component_id": blend_id, "quantity": blend_qty - 0.5}])
    bag_id = _component_id(batch, bag_serial)
    # 追加抽检：放行前的额外抽样同样纳入检验条件。
    batch.record_inspection(request_id=f"{prefix}-ci2", actor_id="qc-1",
                            target_type="component", target_id=bag_id,
                            item="农残追加抽检", method="GC-MS", standard="GB 2763",
                            result="pass", kind="additional")
    _release(batch, f"{prefix}-b2", "component", bag_id)
    batch.issue_material(request_id=f"{prefix}-i3", actor_id="wh-1", order_id=order_id,
                         step_id=f"{order_id}-S3", lot_id="LOT-GLAZE", quantity=2)
    cup_serial = f"CUP-{order_id}"
    batch.report_output(request_id=f"{prefix}-o3", actor_id="factory-1", order_id=order_id,
                        step_id=f"{order_id}-S3", good_quantity=1.8, scrap_quantity=0.2,
                        outputs=[{"serial_no": cup_serial, "component_code": "CUP",
                                  "quantity": 1.8}])
    cup_id = _component_id(batch, cup_serial)
    batch.record_inspection(request_id=f"{prefix}-ci3", actor_id="qc-1",
                            target_type="component", target_id=cup_id,
                            item="铅镉溶出量", method="ICP-MS", standard="GB 4806.4",
                            result="pass")
    _release(batch, f"{prefix}-b3", "component", cup_id)
    batch.issue_material(request_id=f"{prefix}-i4", actor_id="wh-1", order_id=order_id,
                         step_id=f"{order_id}-S4", lot_id="LOT-PACK",
                         quantity=len(unit_serials))
    output = batch.report_output(
        request_id=f"{prefix}-o4", actor_id="factory-1", order_id=order_id,
        step_id=f"{order_id}-S4", good_quantity=len(unit_serials),
        outputs=[{"serial_no": serial} for serial in unit_serials],
        inputs=[{"component_id": bag_id, "quantity": bag_qty},
                {"component_id": cup_id, "quantity": 1.8}])
    output_id = output["output_id"]
    batch.record_inspection(request_id=f"{prefix}-ci4", actor_id="qc-1",
                            target_type="finished_batch", target_id=output_id,
                            item="标签与外观", method="全检", standard="Q/TD 005", result="pass")
    _release(batch, f"{prefix}-b4", "finished_batch", output_id)
    return output_id


def _collect_lots(node: dict, lots: set[str], substitutes: list[dict]) -> None:
    if node.get("type") == "material_lot":
        lots.add(node["id"])
    for parent in node.get("parents", []):
        if parent.get("via", {}).get("relation") == "substitute":
            substitutes.append(parent["via"])
        _collect_lots(parent["node"], lots, substitutes)


def run() -> dict[str, object]:
    """执行完整验收链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        domain = DomainService(database, clock)
        batch = BatchControlService(database, clock)
        _setup(domain)

        recipe = batch.create_recipe(
            request_id="acc-recipe", actor_id="admin-1", product_code="GIFT-SET", version=1,
            lines=[{"material_code": "TEA", "quantity": 0.25, "unit": "kg",
                    "substitutes": ["TEA-ALT"]},
                   {"material_code": "GLAZE", "quantity": 0.05, "unit": "kg"},
                   {"material_code": "PACK", "quantity": 1, "unit": "套"}])
        batch.activate_recipe(request_id="acc-recipe-act", actor_id="admin-1",
                              recipe_id=recipe["recipe_id"])

        _register_lot(batch, "acc-lot-tea", "LOT-TEA", "TEA", "明前龙井", "杭州狮峰", 100, "kg")
        _register_lot(batch, "acc-lot-alt", "LOT-ALT", "TEA-ALT", "高山云雾", "武夷山", 50, "kg")
        _register_lot(batch, "acc-lot-glaze", "LOT-GLAZE", "GLAZE", "青白釉料", "景德镇", 40, "kg")
        _register_lot(batch, "acc-lot-pack", "LOT-PACK", "PACK", "礼盒套装", "苏州", 200, "套")

        # 幂等领料：同一扫码回执重放不会重复扣料。
        batch.create_work_order(request_id="acc-wo0", actor_id="factory-1", order_id="WO-000",
                                site_id="site-pilot", product_code="GIFT-SET",
                                planned_quantity=1, unit="套",
                                steps=[{"name": "配料", "unit": "kg"},
                                       {"name": "包装", "unit": "套"}])
        first = batch.issue_material(request_id="acc-scan-1", actor_id="wh-1", order_id="WO-000",
                                     step_id="WO-000-S1", lot_id="LOT-TEA", quantity=1)
        replay = batch.issue_material(request_id="acc-scan-1", actor_id="wh-1", order_id="WO-000",
                                      step_id="WO-000-S1", lot_id="LOT-TEA", quantity=1)
        lot_tea = batch.get_lot(actor_id="auditor-1", lot_id="LOT-TEA")
        assert not first["replayed"] and replay["replayed"], (first, replay)
        assert replay["issue_id"] == first["issue_id"]
        assert abs(lot_tea["quantity_on_hand"] - 99) < 1e-6, lot_tea

        # 工单一：使用替代料的主工单。
        units_a = [f"UNIT-{index:04d}" for index in range(1, 9)]
        _produce_order(batch, "acc-a", "WO-100", units_a, "LOT-TEA", use_substitute=True)
        # 工单二：只用合格主料的干净工单。
        units_b = [f"UNIT-{index:04d}" for index in range(101, 105)]
        _produce_order(batch, "acc-b", "WO-200", units_b, "LOT-TEA", use_substitute=False)

        batch.create_sales_order(request_id="acc-so1", actor_id="brand-1",
                                 sales_order_id="SO-1", channel="天猫旗舰店",
                                 serials=units_a[:5])
        batch.ship_order(request_id="acc-ship1", actor_id="brand-1",
                         sales_order_id="SO-1", batch_id="CB-1")
        batch.create_sales_order(request_id="acc-so2", actor_id="brand-1",
                                 sales_order_id="SO-2", channel="线下买手店",
                                 serials=[units_b[0]])
        batch.ship_order(request_id="acc-ship2", actor_id="brand-1",
                         sales_order_id="SO-2", batch_id="CB-2")

        conservation = batch.verify_conservation(actor_id="auditor-1", order_id="WO-100")
        assert conservation["balanced"], conservation

        # 污染发现：替代料批次抽检不合格 → 隔离 → 定向冻结。
        batch.record_inspection(request_id="acc-bad", actor_id="qc-1",
                                target_type="material_lot", target_id="LOT-ALT",
                                item="蒽醌残留", method="HPLC", standard="GB 2763",
                                result="fail", kind="additional")
        freeze = batch.create_freeze(request_id="acc-freeze", actor_id="qc-1",
                                     target_type="material_lot", target_id="LOT-ALT",
                                     reason="替代料蒽醌超标")
        frozen_ids = {(item["target_type"], item["target_id"]) for item in freeze["frozen"]}
        assert ("material_lot", "LOT-ALT") in frozen_ids
        assert ("sales_order", "SO-1") in frozen_ids
        assert ("channel_batch", "CB-1") in frozen_ids
        # 未使用替代料的工单、订单与渠道批次不受牵连。
        assert ("sales_order", "SO-2") not in frozen_ids
        assert ("channel_batch", "CB-2") not in frozen_ids
        assert batch.get_lot(actor_id="auditor-1", lot_id="LOT-TEA")["status"] == "available"
        assert batch.get_unit(actor_id="auditor-1", serial_no=units_b[0])["status"] == "shipped"
        assert batch.get_unit(actor_id="auditor-1", serial_no=units_a[0])["status"] == "frozen"

        # 反向追溯：从成品反查全部来源，替代料关系可见。
        trace = batch.trace_backward(actor_id="auditor-1", target_type="finished_unit",
                                     target_id=_unit_id(batch, units_a[0]))
        lots: set[str] = set()
        substitutes: list[dict] = []
        _collect_lots(trace, lots, substitutes)
        assert lots == {"LOT-TEA", "LOT-ALT", "LOT-GLAZE", "LOT-PACK"}, lots
        assert substitutes and substitutes[0]["substituted_for"] == "TEA"

        # 正向召回：范围与数量差异。
        recall = batch.recall_scope(actor_id="auditor-1", target_type="material_lot",
                                    target_id="LOT-ALT")
        assert recall["quantities"]["units_total"] == len(units_a)
        assert len(recall["affected"]["sales_orders"]) == 1
        assert len(recall["affected"]["channel_batches"]) == 1
        assert recall["quantities"]["lots_balanced"]
        assert recall["quantities"]["steps_balanced"]
        assert recall["quantities"]["units_difference"] == 0

        valid, event_count = domain.verify_audit()
        assert valid

        # 重启恢复：关闭后重开同一数据库，工单、隔离与冻结状态继续有效。
        database.close()
        database2 = Database(path)
        domain2 = DomainService(database2, clock)
        batch2 = BatchControlService(database2, clock)
        assert batch2.get_lot(actor_id="auditor-1", lot_id="LOT-ALT")["status"] == "frozen"
        assert batch2.get_freeze(actor_id="auditor-1",
                                 freeze_id=freeze["freeze_id"])["status"] == "active"
        assert batch2.get_work_order(actor_id="auditor-1",
                                     order_id="WO-100")["status"] == "in_progress"
        assert batch2.verify_conservation(actor_id="auditor-1", order_id="WO-100")["balanced"]
        valid2, event_count2 = domain2.verify_audit()
        assert valid2 and event_count2 == event_count
        database2.close()

        return {"status": "ok", "audit_events": event_count, "audit_valid": True,
                "frozen_targets": len(freeze["frozen"]),
                "recall_units": recall["quantities"]["units_total"],
                "restart_verified": True}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

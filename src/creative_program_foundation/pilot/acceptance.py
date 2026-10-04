"""茶·道试产批次控制系统的离线端到端验收。

在临时 SQLite 库中由仓库、质检、工厂、品牌四方走完
登记 → 检验 → 领退料 → 产出守恒 → 双人放行 → 包装 → 精准冻结/召回 → 重启校验，
成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..clock import FixedClock
from ..pilot.service import PilotService
from ..service import DomainService
from ..storage import Database


def run() -> dict[str, object]:
    """执行完整试产控制链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "pilot-acceptance.sqlite3"
        database = Database(db_path)
        clock = FixedClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        domain = DomainService(database, clock)
        pilot = PilotService(database, clock)

        domain.register_organization(request_id="acc-org", actor_id="bootstrap",
                                     organization_id="org", name="茶·道转化团队")
        domain.register_actor(request_id="acc-adm", actor_id="bootstrap", new_actor_id="adm",
                              display_name="管理员", role="admin", organization_id="org")
        for rid, aid, name, role in (
                ("acc-wh", "wh", "仓库", "warehouse"),
                ("acc-qc", "qc", "质检", "qinspector"),
                ("acc-fac", "fac", "工厂", "factory"),
                ("acc-br", "br", "品牌", "brand")):
            domain.register_actor(request_id=rid, actor_id="adm", new_actor_id=aid,
                                  display_name=name, role=role, organization_id="org")

        # 仓库：供应商、两类茶叶（主/替代）与釉料，各自独立交付单和供应证明
        pilot.register_supplier(request_id="acc-sup", actor_id="wh",
                                supplier_id="sup", name="福建合作社")
        for rid, mid, kind, name, unit in (
                ("acc-m1", "tea", "tea", "大红袍", "kg"),
                ("acc-m2", "teasub", "tea", "备用肉桂", "kg"),
                ("acc-m3", "glaze", "glaze", "天目釉", "kg")):
            pilot.register_material(request_id=rid, actor_id="wh", material_id=mid,
                                    kind=kind, name=name, unit=unit)
        batches = {}
        for rid, bid, mat, cert, origin in (
                ("acc-b1", "b-tea", "tea", "CERT-T1", "武夷山"),
                ("acc-b2", "b-sub", "teasub", "CERT-T2", "武夷山二线"),
                ("acc-b3", "b-glz", "glaze", "CERT-G1", "景德镇")):
            pilot.register_material_batch(
                request_id=rid, actor_id="wh", batch_id=bid, material_id=mat,
                supplier_id="sup", delivery_note=f"DN-{bid}", cert_no=cert,
                cert_summary={"inspection": "合格"}, origin_note=origin)
            batches[mat] = bid
        lots = {}
        for rid, mat, label, qty in (
                ("acc-l1", "tea", "LOT-TEA", 100),
                ("acc-l2", "teasub", "LOT-SUB", 20),
                ("acc-l3", "glaze", "LOT-GLZ", 30)):
            lots[mat] = pilot.receive_lot(
                request_id=rid, actor_id="wh", batch_id=batches[mat],
                label=label, qty=qty).resource_id

        # 质检：检验规范与入库检验
        for rid, sid, ref in (
                ("acc-s1", "s-tea", "tea"),
                ("acc-s2", "s-sub", "teasub"),
                ("acc-s3", "s-glz", "glaze")):
            pilot.register_inspection_spec(
                request_id=rid, actor_id="qc", spec_id=sid, applies_to="material",
                ref_id=ref, version=1, items=["农残", "重金属"])
        for rid, mat, sid in (
                ("acc-i1", "tea", "s-tea"),
                ("acc-i2", "teasub", "s-sub"),
                ("acc-i3", "glaze", "s-glz")):
            pilot.inspect_lot(request_id=rid, actor_id="qc", lot_id=lots[mat],
                              spec_id=sid, decision="pass")

        # 品牌：两道工序配方与组件检验规范
        pilot.register_recipe(
            request_id="acc-recipe", actor_id="br", recipe_id="rec",
            product_code="CHADAO-SET", version=1,
            lines=[
                {"line_seq": 1, "step_seq": 1, "input_kind": "material",
                 "input_ref_id": "tea", "qty_per": 1.0, "allow_substitute": True},
                {"line_seq": 2, "step_seq": 1, "input_kind": "material",
                 "input_ref_id": "glaze", "qty_per": 0.5, "allow_substitute": False},
            ])
        pilot.register_inspection_spec(
            request_id="acc-sc", actor_id="qc", spec_id="s-comp", applies_to="component",
            ref_id="CHADAO-SET", version=1, items=["外观", "密封"])
        pilot.register_order(request_id="acc-order", actor_id="br", order_id="ord",
                             channel="体验店", product_code="CHADAO-SET", qty=5)
        pilot.register_channel_batch(request_id="acc-cb", actor_id="br", batch_id="cb",
                                     order_id="ord", qty_planned=5)

        # 工厂开工单（排产 8 件），仓库发料：首批 5 主料 + 1 替代料
        pilot.open_work_order(request_id="acc-wo", actor_id="fac", wo_id="wo",
                              recipe_id="rec", planned_qty=8)
        pilot.issue_material(request_id="acc-iss1", actor_id="wh", wo_id="wo", line_seq=1,
                             lot_id=lots["tea"], qty=4, scan_ref="ACC-SCAN-1")
        pilot.issue_material(request_id="acc-iss2", actor_id="wh", wo_id="wo", line_seq=1,
                             lot_id=lots["teasub"], qty=2, scan_ref="ACC-SCAN-2")
        pilot.issue_material(request_id="acc-iss3", actor_id="wh", wo_id="wo", line_seq=2,
                             lot_id=lots["glaze"], qty=3, scan_ref="ACC-SCAN-3")
        # 重复扫码不重复扣料
        try:
            pilot.issue_material(request_id="acc-iss-dup", actor_id="wh", wo_id="wo",
                                 line_seq=1, lot_id=lots["tea"], qty=4, scan_ref="ACC-SCAN-1")
            dup_blocked = False
        except Exception:
            dup_blocked = True

        # 工序一：4 合格、1 报废、1 返工（报废件与返工件用替代料）
        usage = [{"1": lots["tea"], "2": lots["glaze"]}] * 4
        usage += [{"1": lots["teasub"], "2": lots["glaze"]}] * 2
        out1 = pilot.report_output(
            request_id="acc-out1", actor_id="fac", wo_id="wo", step_seq=1,
            qty_good=4, qty_scrap=1, qty_rework=1,
            materials=[{"line_seq": 1, "lot_id": lots["tea"], "qty": 4},
                       {"line_seq": 1, "lot_id": lots["teasub"], "qty": 2},
                       {"line_seq": 2, "lot_id": lots["glaze"], "qty": 3}],
            material_usage=usage)
        step1_good = out1.response["components"]
        step1_rework = out1.response["rework_components"]

        # 先领料后补报损耗：计划尚有余量，再领 1 单位主料，补报 1 单位在制损耗
        pilot.issue_material(request_id="acc-iss4", actor_id="wh", wo_id="wo", line_seq=1,
                             lot_id=lots["tea"], qty=1, scan_ref="ACC-SCAN-4")
        pilot.report_wip_scrap(request_id="acc-scrap", actor_id="fac", wo_id="wo",
                               lot_id=lots["tea"], qty=1, note="车间撒漏补报")

        # 检验 + 双人放行
        def release(cid: str, suffix: str) -> None:
            pilot.inspect_component(request_id=f"acc-ic-{suffix}", actor_id="qc",
                                    component_id=cid, spec_id="s-comp", decision="pass")
            ticket = pilot.request_release(request_id=f"acc-rr-{suffix}", actor_id="fac",
                                           component_id=cid).resource_id
            pilot.approve_release(request_id=f"acc-aq-{suffix}", actor_id="qc",
                                  ticket_id=ticket, role="qinspector")
            pilot.approve_release(request_id=f"acc-ab-{suffix}", actor_id="br",
                                  ticket_id=ticket, role="brand")

        for index, cid in enumerate(step1_good):
            release(cid, f"g{index}")
        # 返工件原位修复后放行
        rwid = database.connection.execute(
            "SELECT rework_id FROM rework_jobs WHERE source_component_id=?",
            (step1_rework[0],)).fetchone()["rework_id"]
        pilot.finish_rework(request_id="acc-rwf", actor_id="fac", rework_id=rwid,
                            decision="repaired")
        release(step1_rework[0], "rw")

        # 工序二：5 个放行组件投入，4 合格 1 报废
        inputs = step1_good + step1_rework
        out2 = pilot.report_output(
            request_id="acc-out2", actor_id="fac", wo_id="wo", step_seq=2,
            qty_good=4, qty_scrap=1, qty_rework=0, input_components=inputs)
        finals = out2.response["components"]
        for index, cid in enumerate(finals):
            release(cid, f"f{index}")
        pilot.pack_package(request_id="acc-pack", actor_id="br", channel_batch_id="cb",
                           component_ids=finals)

        # 守恒核对
        wo_balance = pilot.verify_work_order_balance("wo")
        lot_balances = {mat: pilot.verify_lot_balance(lot).balanced
                        for mat, lot in lots.items()}
        stock = {mat: pilot.get_lot(lot)["remaining_qty"] for mat, lot in lots.items()}

        # 反向追溯
        trace = pilot.trace_component(finals[0])
        traced_lots = {n["entity_id"] for n in trace["nodes"]
                       if n["entity_type"] == "lot"}
        traced_cert = any(n["detail"].get("cert_no") == "CERT-T1"
                          for n in trace["nodes"] if n["entity_type"] == "lot")

        # 替代料污染：冻结 3 个组件链（工序一报废件、返工件、返工件在工序二的报废件），
        # 4 件合格成品、包装与订单均不受影响
        pilot.raise_freeze(request_id="acc-fz", actor_id="qc", source_kind="lot",
                           source_id=lots["teasub"], reason="contamination",
                           note="追加检测农残异常")
        recall = pilot.recall_scope(source_kind="lot", source_id=lots["teasub"])
        order_status = database.connection.execute(
            "SELECT status FROM orders WHERE order_id='ord'").fetchone()[0]
        package_status = database.connection.execute(
            "SELECT status FROM packages").fetchone()[0]

        audit_valid, audit_count = domain.verify_audit()

        # 重启：未完成工单与冻结状态继续有效
        database.close()
        database = Database(db_path)
        restarted = PilotService(database)
        wo_after = restarted.get_work_order("wo")["status"]
        frozen_after = restarted.get_lot(lots["teasub"])["status"]
        freezes_after = len(restarted.list_freeze_events(active_only=True))
        balance_after = restarted.verify_work_order_balance("wo")["balanced"]
        database.close()

        return {
            "status": "ok",
            "dup_scan_blocked": dup_blocked,
            "stock": stock,
            "wo_balanced": wo_balance["balanced"],
            "lot_balances": lot_balances,
            "steps": [s["balanced"] for s in wo_balance["steps"]],
            "trace_has_substitute_lot": lots["teasub"] in traced_lots,
            "trace_has_cert": traced_cert,
            "recall_components": len(recall.components),
            "recall_packages": len(recall.packages),
            "order_status_after_targeted_freeze": order_status,
            "package_status_after_targeted_freeze": package_status,
            "wo_after_restart": wo_after,
            "frozen_after_restart": frozen_after,
            "freezes_after_restart": freezes_after,
            "balanced_after_restart": balance_after,
            "audit_valid": audit_valid,
            "audit_events": audit_count,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (
        result["status"] == "ok"
        and result["dup_scan_blocked"]
        and result["wo_balanced"]
        and all(result["lot_balances"].values())
        and all(result["steps"])
        and not result["trace_has_substitute_lot"]
        and result["trace_has_cert"]
        and result["recall_components"] == 3
        and result["recall_packages"] == 0
        and result["order_status_after_targeted_freeze"] == "open"
        and result["package_status_after_targeted_freeze"] == "sealed"
        and result["wo_after_restart"] == "in_progress"
        and result["frozen_after_restart"] == "frozen"
        and result["freezes_after_restart"] == 1
        and result["balanced_after_restart"]
        and result["audit_valid"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

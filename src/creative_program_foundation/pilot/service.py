"""茶·道试产批次控制领域服务。

覆盖原料批次与供应证明、检验、领退料与补报损耗、工序产出与守恒、
组件序列与谱系、双人质量放行、包装组合、污染/标签错误的精准冻结，
以及正反向追溯。所有写操作都在 IMMEDIATE 短事务内完成并写入哈希审计链。
"""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Callable

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from .models import FreezeImpact, LineageNode, PilotReceipt, StockSnapshot
from .storage import ensure_pilot_schema

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
QTY_EPS = 1e-6

ROLE_WAREHOUSE = "warehouse"
ROLE_QC = "qinspector"
ROLE_FACTORY = "factory"
ROLE_BRAND = "brand"


def _qty(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(f"{field} 必须是正数")
    return number


def _nonneg(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是数字") from exc
    if not math.isfinite(number) or number < 0:
        raise ValidationError(f"{field} 不能为负数")
    return number


class PilotService:
    """实现试产批次控制的全部事务规则。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_pilot_schema(database)

    # ------------------------------------------------------------------
    # 通用基础设施
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, conn, actor_id: str) -> Actor:
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _replay_receipt(self, conn, request_id: str, action: str,
                        payload: dict[str, Any]) -> PilotReceipt | None:
        """事务入口处的幂等早回放：状态已变化时也能返回原始回执。"""

        request_id = self._id(request_id, "request_id")
        row = conn.execute("SELECT * FROM pilot_receipts WHERE request_id=?",
                           (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return PilotReceipt(request_id, row["resource_type"], row["resource_id"], True,
                            json.loads(row["response_json"]))

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> PilotReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM pilot_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return PilotReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO pilot_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return PilotReceipt(request_id, resource_type, resource_id, False, response)

    def _audit(self, conn, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _scan_already_used(self, conn, scan_ref: str, request_id: str) -> bool:
        """扫码已被其他请求处理时返回 True；当前请求自身的重放走幂等回执。"""

        return conn.execute(
            "SELECT 1 FROM material_transactions WHERE scan_ref=? "
            "AND NOT EXISTS (SELECT 1 FROM pilot_receipts WHERE request_id=?)",
            (scan_ref, request_id),
        ).fetchone() is not None

    def _get(self, conn, table: str, key_field: str, key_value: str, label: str):
        row = conn.execute(f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)).fetchone()
        if row is None:
            raise NotFoundError(f"{label}不存在")
        return row

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    # ------------------------------------------------------------------
    # 供应商 / 物料 / 原料批次 / 交付与供应证明
    # ------------------------------------------------------------------

    def register_supplier(self, *, request_id: str, actor_id: str,
                          supplier_id: str, name: str) -> PilotReceipt:
        payload = {"actor_id": actor_id, "supplier_id": supplier_id, "name": name}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_supplier", payload)
            if _replay is not None:
                return _replay
            supplier_id = self._id(supplier_id, "supplier_id")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO suppliers(supplier_id,name,created_by,created_at) VALUES(?,?,?,?)",
                        (supplier_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("供应商编号已存在") from exc
                self._audit(conn, actor_id, "pilot.supplier.registered", "supplier",
                            supplier_id, {"name": name})
                return "supplier", supplier_id, {"supplier_id": supplier_id}

            return self._idempotent(conn, request_id=request_id, action="register_supplier",
                                    payload=payload, create=create)

    def register_material(self, *, request_id: str, actor_id: str, material_id: str,
                          kind: str, name: str, unit: str) -> PilotReceipt:
        payload = {"actor_id": actor_id, "material_id": material_id, "kind": kind,
                   "name": name, "unit": unit}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_material", payload)
            if _replay is not None:
                return _replay
            material_id = self._id(material_id, "material_id")
            name = self._text(name, "name")
            unit = self._text(unit, "unit", 20)
            if kind not in ("tea", "glaze", "packaging", "other"):
                raise ValidationError("kind 必须是 tea/glaze/packaging/other")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO materials(material_id,kind,name,unit,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (material_id, kind, name, unit, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("物料编号已存在") from exc
                self._audit(conn, actor_id, "pilot.material.registered", "material",
                            material_id, {"kind": kind, "name": name, "unit": unit})
                return "material", material_id, {"material_id": material_id}

            return self._idempotent(conn, request_id=request_id, action="register_material",
                                    payload=payload, create=create)

    def register_material_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                material_id: str, supplier_id: str, delivery_note: str,
                                cert_no: str, cert_summary: dict[str, Any],
                                origin_note: str) -> PilotReceipt:
        """登记一张交付单中的一个原料批次及其供应证明。

        不同产地/供应商的物料必须分批登记，不允许混在同一批次中。
        """

        if not isinstance(cert_summary, dict) or not cert_summary:
            raise ValidationError("cert_summary 必须是非空对象")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "material_id": material_id,
                   "supplier_id": supplier_id, "delivery_note": delivery_note,
                   "cert_no": cert_no, "cert_summary": cert_summary, "origin_note": origin_note}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            _replay = self._replay_receipt(conn, request_id, "register_material_batch", payload)
            if _replay is not None:
                return _replay
            batch_id = self._id(batch_id, "batch_id")
            delivery_note = self._text(delivery_note, "delivery_note")
            cert_no = self._text(cert_no, "cert_no", 120)
            origin_note = self._text(origin_note, "origin_note")
            self._get(conn, "materials", "material_id", material_id, "物料")
            self._get(conn, "suppliers", "supplier_id", supplier_id, "供应商")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO material_batches(batch_id,material_id,supplier_id,delivery_note,"
                        "cert_no,cert_summary_json,origin_note,received_at,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, material_id, supplier_id, delivery_note, cert_no,
                         canonical_json(cert_summary), origin_note, self._now(),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("原料批次编号已存在") from exc
                self._audit(conn, actor_id, "pilot.material_batch.registered", "material_batch",
                            batch_id, {"material_id": material_id, "supplier_id": supplier_id,
                                       "delivery_note": delivery_note, "cert_no": cert_no,
                                       "origin_note": origin_note})
                return "material_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(conn, request_id=request_id, action="register_material_batch",
                                    payload=payload, create=create)

    def receive_lot(self, *, request_id: str, actor_id: str, batch_id: str,
                    label: str, qty: float) -> PilotReceipt:
        """批次到货入库，生成可追溯的库存批次（lot），默认隔离待检。"""

        qty = _qty(qty, "qty")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "label": label, "qty": qty}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            _replay = self._replay_receipt(conn, request_id, "receive_lot", payload)
            if _replay is not None:
                return _replay
            batch = self._get(conn, "material_batches", "batch_id", batch_id, "原料批次")
            label = self._text(label, "label", 120)

            def create():
                lot_id = self._new_id()
                conn.execute(
                    "INSERT INTO material_lots(lot_id,batch_id,label,qty_received,remaining_qty,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?, 'quarantine', ?,?)",
                    (lot_id, batch_id, label, qty, qty, actor_id, self._now()),
                )
                conn.execute(
                    "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,qty_delta,"
                    "ref_type,ref_id,scan_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (self._new_id(), None, None, lot_id, "receive", qty,
                     "material_batch", batch_id, None, actor_id, self._now()),
                )
                self._assert_lot_balance(conn, lot_id)
                self._audit(conn, actor_id, "pilot.lot.received", "lot", lot_id,
                            {"batch_id": batch_id, "label": label, "qty": qty})
                return "lot", lot_id, {"lot_id": lot_id, "batch_id": batch_id, "qty": qty}

            return self._idempotent(conn, request_id=request_id, action="receive_lot",
                                    payload=payload, create=create)

    def repack_lot(self, *, request_id: str, actor_id: str,
                   inputs: list[dict[str, Any]], outputs: list[dict[str, Any]],
                   note: str) -> PilotReceipt:
        """拆包或重组库存批次。

        投入总量必须等于产出总量；新批次通过 lot_lineage 保留与原批次的谱系，
        原料来源不会因为换包装/换标签而丢失。
        """

        note = self._text(note, "note")
        normalized_in, normalized_out = [], []
        for item in inputs or []:
            normalized_in.append({"lot_id": self._id(item.get("lot_id", ""), "inputs.lot_id"),
                                  "qty": _qty(item.get("qty"), "inputs.qty")})
        for item in outputs or []:
            normalized_out.append({"label": self._text(item.get("label", ""), "outputs.label", 120),
                                   "qty": _qty(item.get("qty"), "outputs.qty")})
        if not normalized_in or not normalized_out:
            raise ValidationError("inputs 和 outputs 都不能为空")
        total_in = sum(item["qty"] for item in normalized_in)
        total_out = sum(item["qty"] for item in normalized_out)
        if not math.isclose(total_in, total_out, abs_tol=QTY_EPS):
            raise ValidationError("拆包重组投入量与产出量不守恒")
        payload = {"actor_id": actor_id, "inputs": normalized_in,
                   "outputs": normalized_out, "note": note}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            _replay = self._replay_receipt(conn, request_id, "repack_lot", payload)
            if _replay is not None:
                return _replay
            rows = [self._get(conn, "material_lots", "lot_id", item["lot_id"], "库存批次")
                    for item in normalized_in]
            for row, item in zip(rows, normalized_in):
                if row["status"] == "frozen":
                    raise ConflictError(f"库存批次 {item['lot_id']} 已冻结，不能拆包重组")
                if row["remaining_qty"] + QTY_EPS < item["qty"]:
                    raise ConflictError(f"库存批次 {item['lot_id']} 余量不足")
            batch_material_ids = set()
            for row in rows:
                batch = self._get(conn, "material_batches", "batch_id",
                                  row["batch_id"], "原料批次")
                batch_material_ids.add(batch["material_id"])
            if len(batch_material_ids) > 1:
                raise ValidationError("不同物料的库存批次不能合并重组")
            child_available = all(r["status"] == "available" for r in rows)
            reason = "split" if len(normalized_in) == 1 else "merge"

            def create():
                input_ids = []
                for row, item in zip(rows, normalized_in):
                    conn.execute(
                        "UPDATE material_lots SET remaining_qty=remaining_qty-? WHERE lot_id=?",
                        (item["qty"], item["lot_id"]),
                    )
                    conn.execute(
                        "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,"
                        "qty_delta,ref_type,ref_id,scan_ref,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (self._new_id(), None, None, item["lot_id"], "repack_out", -item["qty"],
                         "repack", note, None, actor_id, self._now()),
                    )
                    self._assert_lot_balance(conn, item["lot_id"])
                    input_ids.append(item["lot_id"])
                output_ids = []
                for item in normalized_out:
                    child = self._new_id()
                    conn.execute(
                        "INSERT INTO material_lots(lot_id,batch_id,label,qty_received,"
                        "remaining_qty,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (child, rows[0]["batch_id"], item["label"], 0, item["qty"],
                         "available" if child_available else "quarantine", actor_id, self._now()),
                    )
                    conn.execute(
                        "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,"
                        "qty_delta,ref_type,ref_id,scan_ref,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (self._new_id(), None, None, child, "repack_in", item["qty"],
                         "repack", note, None, actor_id, self._now()),
                    )
                    for parent_id, parent in zip(input_ids, normalized_in):
                        share = (parent["qty"] / total_in) * item["qty"]
                        conn.execute(
                            "INSERT INTO lot_lineage(lineage_id,parent_lot_id,child_lot_id,reason,"
                            "qty,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                            (self._new_id(), parent_id, child, reason, share, actor_id, self._now()),
                        )
                    self._assert_lot_balance(conn, child)
                    output_ids.append(child)
                self._audit(conn, actor_id, "pilot.lot.repacked", "repack", output_ids[0],
                            {"inputs": input_ids, "outputs": output_ids,
                             "reason": reason, "note": note})
                return "repack", output_ids[0], {"lot_ids": output_ids, "reason": reason}

            return self._idempotent(conn, request_id=request_id, action="repack_lot",
                                    payload=payload, create=create)

    def relabel_lot(self, *, request_id: str, actor_id: str, lot_id: str,
                    new_label: str, reason: str) -> PilotReceipt:
        """更正库存批次标签，旧标签永久保留在 lot_relabels 中。"""

        payload = {"actor_id": actor_id, "lot_id": lot_id, "new_label": new_label, "reason": reason}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE, ROLE_QC)
            _replay = self._replay_receipt(conn, request_id, "relabel_lot", payload)
            if _replay is not None:
                return _replay
            lot_id = self._id(lot_id, "lot_id")
            new_label = self._text(new_label, "new_label", 120)
            reason = self._text(reason, "reason")
            lot = self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")

            def create():
                relabel_id = self._new_id()
                conn.execute(
                    "INSERT INTO lot_relabels(relabel_id,lot_id,old_label,new_label,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (relabel_id, lot_id, lot["label"], new_label, reason, actor_id, self._now()),
                )
                conn.execute("UPDATE material_lots SET label=? WHERE lot_id=?",
                             (new_label, lot_id))
                self._audit(conn, actor_id, "pilot.lot.relabelled", "lot", lot_id,
                            {"old_label": lot["label"], "new_label": new_label, "reason": reason})
                return "lot", lot_id, {"lot_id": lot_id, "label": new_label}

            return self._idempotent(conn, request_id=request_id, action="relabel_lot",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 检验项目与检验结论
    # ------------------------------------------------------------------

    def register_inspection_spec(self, *, request_id: str, actor_id: str, spec_id: str,
                                 applies_to: str, ref_id: str, version: int,
                                 items: list[str]) -> PilotReceipt:
        """登记检验规范版本（原料检验或组件/成品检验）。"""

        if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
            raise ValidationError("items 必须是非空字符串列表")
        version = int(version)
        payload = {"actor_id": actor_id, "spec_id": spec_id, "applies_to": applies_to,
                   "ref_id": ref_id, "version": version, "items": items}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_inspection_spec", payload)
            if _replay is not None:
                return _replay
            spec_id = self._id(spec_id, "spec_id")
            ref_id = self._id(ref_id, "ref_id")
            if applies_to not in ("material", "component"):
                raise ValidationError("applies_to 必须是 material 或 component")
            if version < 1:
                raise ValidationError("version 必须 >= 1")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO inspection_specs(spec_id,applies_to,ref_id,version,items_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (spec_id, applies_to, ref_id, version, canonical_json(items),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("检验规范版本已存在") from exc
                self._audit(conn, actor_id, "pilot.inspection_spec.registered", "inspection_spec",
                            spec_id, {"applies_to": applies_to, "ref_id": ref_id,
                                      "version": version, "items": items})
                return "inspection_spec", spec_id, {"spec_id": spec_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="register_inspection_spec", payload=payload, create=create)

    def _latest_spec(self, conn, applies_to: str, ref_id: str, version: int | None = None):
        if version is None:
            row = conn.execute(
                "SELECT * FROM inspection_specs WHERE applies_to=? AND ref_id=? "
                "ORDER BY version DESC LIMIT 1", (applies_to, ref_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM inspection_specs WHERE applies_to=? AND ref_id=? AND version=?",
                (applies_to, ref_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("检验规范不存在")
        return row

    def inspect_lot(self, *, request_id: str, actor_id: str, lot_id: str, spec_id: str,
                    decision: str, findings: dict[str, Any] | None = None,
                    is_additional: bool = False) -> PilotReceipt:
        """原料入库检验或追加抽检。

        首次合格后批次才可发料；已可用批次的追加抽检不合格会立即触发
        仅针对该批次（及其真实下游）的冻结事件。
        """

        findings = findings or {}
        if not isinstance(findings, dict):
            raise ValidationError("findings 必须是对象")
        if decision not in ("pass", "fail"):
            raise ValidationError("decision 必须是 pass 或 fail")
        payload = {"actor_id": actor_id, "lot_id": lot_id, "spec_id": spec_id,
                   "decision": decision, "findings": findings, "is_additional": is_additional}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC)
            _replay = self._replay_receipt(conn, request_id, "inspect_lot", payload)
            if _replay is not None:
                return _replay
            lot_id = self._id(lot_id, "lot_id")
            spec_id = self._id(spec_id, "spec_id")
            lot = self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")
            batch = self._get(conn, "material_batches", "batch_id", lot["batch_id"], "原料批次")
            spec = self._get(conn, "inspection_specs", "spec_id", spec_id, "检验规范")
            if spec["applies_to"] != "material" or spec["ref_id"] != batch["material_id"]:
                raise ValidationError("检验规范与物料不匹配")

            def create():
                result_id = self._new_id()
                conn.execute(
                    "INSERT INTO inspection_results(result_id,spec_id,target_kind,target_id,"
                    "is_additional,decision,inspector_id,findings_json,created_at) "
                    "VALUES(?,?, 'lot', ?,?,?,?,?,?)",
                    (result_id, spec_id, lot_id, 1 if is_additional else 0,
                     decision, actor_id, canonical_json(findings), self._now()),
                )
                if decision == "pass" and lot["status"] == "quarantine":
                    conn.execute("UPDATE material_lots SET status='available' WHERE lot_id=?",
                                 (lot_id,))
                self._audit(conn, actor_id, "pilot.lot.inspected", "lot", lot_id,
                            {"spec_id": spec_id, "decision": decision,
                             "is_additional": is_additional, "findings": findings})
                if decision == "fail" and (is_additional or lot["status"] == "available"):
                    self._apply_freeze(conn, actor_id=actor_id, source_kind="lot",
                                       source_id=lot_id, reason="inspection_fail",
                                       note=f"追加抽检不合格: {canonical_json(findings)[:200]}")
                return "inspection_result", result_id, {"result_id": result_id, "decision": decision}

            return self._idempotent(conn, request_id=request_id, action="inspect_lot",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 配方版本
    # ------------------------------------------------------------------

    def register_recipe(self, *, request_id: str, actor_id: str, recipe_id: str,
                        product_code: str, version: int, lines: list[dict[str, Any]],
                        note: str = "", activate: bool = True) -> PilotReceipt:
        """登记不可变配方版本。

        lines: [{line_seq, step_seq, input_kind(material/product), input_ref_id,
                 qty_per, allow_substitute}]
        """

        version = int(version)
        note = str(note or "").strip()
        normalized, seqs, steps = [], set(), set()
        for line in lines or []:
            line_seq = int(line["line_seq"])
            step_seq = int(line["step_seq"])
            input_kind = str(line["input_kind"])
            input_ref_id = self._id(line.get("input_ref_id", ""), "input_ref_id")
            qty_per = _qty(line.get("qty_per"), "qty_per")
            allow_sub = bool(line.get("allow_substitute", False))
            if input_kind not in ("material", "product"):
                raise ValidationError("input_kind 必须是 material 或 product")
            if line_seq in seqs or line_seq < 1 or step_seq < 1:
                raise ValidationError("line_seq 必须唯一且从 1 开始")
            seqs.add(line_seq)
            steps.add(step_seq)
            normalized.append({"line_seq": line_seq, "step_seq": step_seq,
                               "input_kind": input_kind, "input_ref_id": input_ref_id,
                               "qty_per": qty_per, "allow_substitute": allow_sub})
        if not normalized:
            raise ValidationError("配方至少要有一行")
        payload = {"actor_id": actor_id, "recipe_id": recipe_id, "product_code": product_code,
                   "version": version, "lines": normalized, "note": note, "activate": activate}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_recipe", payload)
            if _replay is not None:
                return _replay
            recipe_id = self._id(recipe_id, "recipe_id")
            product_code = self._id(product_code, "product_code")
            if version < 1:
                raise ValidationError("version 必须 >= 1")
            for line in normalized:
                if line["input_kind"] == "material":
                    self._get(conn, "materials", "material_id", line["input_ref_id"], "配方物料")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO recipes(recipe_id,product_code,version,active,note,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (recipe_id, product_code, version, 1 if activate else 0,
                         note, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("配方编号已存在") from exc
                for line in normalized:
                    conn.execute(
                        "INSERT INTO recipe_lines(recipe_id,line_seq,step_seq,input_kind,"
                        "input_ref_id,qty_per,allow_substitute) VALUES(?,?,?,?,?,?,?)",
                        (recipe_id, line["line_seq"], line["step_seq"], line["input_kind"],
                         line["input_ref_id"], line["qty_per"],
                         1 if line["allow_substitute"] else 0),
                    )
                self._audit(conn, actor_id, "pilot.recipe.registered", "recipe", recipe_id,
                            {"product_code": product_code, "version": version,
                             "steps": sorted(steps), "line_count": len(normalized)})
                return "recipe", recipe_id, {"recipe_id": recipe_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="register_recipe",
                                    payload=payload, create=create)

    def activate_recipe(self, *, request_id: str, actor_id: str, product_code: str,
                        version: int) -> PilotReceipt:
        payload = {"actor_id": actor_id, "product_code": product_code, "version": version}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "activate_recipe", payload)
            if _replay is not None:
                return _replay
            product_code = self._id(product_code, "product_code")
            row = conn.execute(
                "SELECT * FROM recipes WHERE product_code=? AND version=?",
                (product_code, int(version))).fetchone()
            if row is None:
                raise NotFoundError("配方版本不存在")

            def create():
                conn.execute("UPDATE recipes SET active=0 WHERE product_code=?", (product_code,))
                conn.execute("UPDATE recipes SET active=1 WHERE recipe_id=?", (row["recipe_id"],))
                self._audit(conn, actor_id, "pilot.recipe.activated", "recipe",
                            row["recipe_id"], {"product_code": product_code, "version": int(version)})
                return "recipe", row["recipe_id"], {"recipe_id": row["recipe_id"], "active": True}

            return self._idempotent(conn, request_id=request_id, action="activate_recipe",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 工单与领退料
    # ------------------------------------------------------------------

    def open_work_order(self, *, request_id: str, actor_id: str, wo_id: str,
                        recipe_id: str, planned_qty: float) -> PilotReceipt:
        planned_qty = _qty(planned_qty, "planned_qty")
        payload = {"actor_id": actor_id, "wo_id": wo_id, "recipe_id": recipe_id,
                   "planned_qty": planned_qty}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_FACTORY, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "open_work_order", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            recipe = self._get(conn, "recipes", "recipe_id",
                               self._id(recipe_id, "recipe_id"), "配方")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO work_orders(wo_id,recipe_id,product_code,recipe_version,"
                        "planned_qty,status,opened_by,created_at,closed_at) "
                        "VALUES(?,?,?,?,?, 'open', ?,?,NULL)",
                        (wo_id, recipe["recipe_id"], recipe["product_code"], recipe["version"],
                         planned_qty, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("工单号已存在") from exc
                self._audit(conn, actor_id, "pilot.wo.opened", "work_order", wo_id,
                            {"recipe_id": recipe["recipe_id"],
                             "recipe_version": recipe["version"], "planned_qty": planned_qty})
                return "work_order", wo_id, {"wo_id": wo_id, "status": "open"}

            return self._idempotent(conn, request_id=request_id, action="open_work_order",
                                    payload=payload, create=create)

    def _recipe_line(self, conn, wo, line_seq: int):
        row = conn.execute(
            "SELECT rl.*, r.version AS recipe_version FROM recipe_lines rl "
            "JOIN recipes r ON r.recipe_id=rl.recipe_id "
            "WHERE rl.recipe_id=? AND rl.line_seq=?",
            (wo["recipe_id"], line_seq),
        ).fetchone()
        if row is None:
            raise ValidationError(f"配方行 {line_seq} 不存在")
        if row["recipe_version"] != wo["recipe_version"]:
            raise ConflictError("工单配方版本与配方行不一致")
        return row

    def issue_material(self, *, request_id: str, actor_id: str, wo_id: str, line_seq: int,
                       lot_id: str, qty: float, scan_ref: str) -> PilotReceipt:
        """仓库按工单配料扫码发料。

        - 库存批次必须检验合格且未冻结；
        - 物料必须匹配配方行，替代料要求该行 allow_substitute；
        - 余量不足直接拒绝（事务串行化，并发领料不会出现负库存）；
        - scan_ref 全局唯一，重复扫码绝不重复扣料；
        - 累计领用量不能超过工单计划需求量。
        """

        qty = _qty(qty, "qty")
        scan_ref = self._id(scan_ref, "scan_ref")
        payload = {"actor_id": actor_id, "wo_id": wo_id, "line_seq": line_seq,
                   "lot_id": lot_id, "qty": qty, "scan_ref": scan_ref}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            _replay = self._replay_receipt(conn, request_id, "issue_material", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            lot_id = self._id(lot_id, "lot_id")
            line_seq = int(line_seq)
            wo = self._get(conn, "work_orders", "wo_id", wo_id, "工单")
            if wo["status"] in ("completed", "closed"):
                raise ConflictError("工单已结案，不能领料")
            if wo["status"] == "frozen":
                raise ConflictError("工单已冻结，不能领料")
            line = self._recipe_line(conn, wo, line_seq)
            lot = self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")
            if lot["status"] != "available":
                raise ConflictError("库存批次未检验合格或已冻结，不能发料")
            batch = self._get(conn, "material_batches", "batch_id", lot["batch_id"], "原料批次")
            substitute_for = None
            if line["input_kind"] != "material" or batch["material_id"] != line["input_ref_id"]:
                if not line["allow_substitute"]:
                    raise ConflictError("该物料不是配方行指定物料，且配方不允许替代料")
                substitute_for = line["input_ref_id"]
            if self._scan_already_used(conn, scan_ref, request_id):
                raise ConflictError("该扫码已经处理，不能重复扣料")
            requirement = line["qty_per"] * wo["planned_qty"]
            allocated = conn.execute(
                "SELECT COALESCE(SUM(allocated_qty),0) AS s FROM kit_allocations WHERE wo_id=? AND line_seq=?",
                (wo_id, line_seq),
            ).fetchone()["s"]
            if allocated + qty > requirement + QTY_EPS:
                raise ConflictError(
                    f"领料量 {allocated + qty} 超过计划需求 {requirement}")
            if lot["remaining_qty"] + QTY_EPS < qty:
                raise ConflictError(f"库存批次余量 {lot['remaining_qty']} 不足 {qty}")

            def create():
                txn_id = self._new_id()
                conn.execute(
                    "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,qty_delta,"
                    "ref_type,ref_id,scan_ref,created_by,created_at) "
                    "VALUES(?,?,?,?, 'issue', ?,?,?,?,?,?)",
                    (txn_id, wo_id, line_seq, lot_id, -qty, "work_order", wo_id,
                     scan_ref, actor_id, self._now()),
                )
                conn.execute("UPDATE material_lots SET remaining_qty=remaining_qty-? WHERE lot_id=?",
                             (qty, lot_id))
                existing = conn.execute(
                    "SELECT 1 FROM kit_allocations WHERE wo_id=? AND line_seq=? AND lot_id=?",
                    (wo_id, line_seq, lot_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE kit_allocations SET allocated_qty=allocated_qty+? "
                        "WHERE wo_id=? AND line_seq=? AND lot_id=?",
                        (qty, wo_id, line_seq, lot_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO kit_allocations(wo_id,line_seq,lot_id,"
                        "substitute_for_material_id,allocated_qty) VALUES(?,?,?,?,?)",
                        (wo_id, line_seq, lot_id, substitute_for, qty),
                    )
                self._assert_lot_balance(conn, lot_id)
                self._audit(conn, actor_id, "pilot.material.issued", "lot", lot_id,
                            {"wo_id": wo_id, "line_seq": line_seq, "qty": qty,
                             "scan_ref": scan_ref,
                             "substitute_for_material_id": substitute_for})
                return "material_transaction", txn_id, {"txn_id": txn_id, "remaining_qty": lot["remaining_qty"] - qty}

            return self._idempotent(conn, request_id=request_id, action="issue_material",
                                    payload=payload, create=create)

    def _wip_qty(self, conn, wo_id: str, lot_id: str) -> float:
        row = conn.execute(
            "SELECT "
            " COALESCE(SUM(CASE WHEN kind='issue' THEN -qty_delta END),0) AS issued, "
            " COALESCE(SUM(CASE WHEN kind='return' THEN qty_delta END),0) AS returned, "
            " COALESCE(SUM(CASE WHEN kind='wip_scrap' THEN -qty_delta END),0) AS scrapped "
            "FROM material_transactions WHERE wo_id=? AND lot_id=? AND kind IN ('issue','return','wip_scrap')",
            (wo_id, lot_id),
        ).fetchone()
        consumed = conn.execute(
            "SELECT COALESCE(SUM(mc.qty),0) AS s FROM material_consumption mc "
            "JOIN process_outputs po ON po.output_id=mc.output_id WHERE po.wo_id=? AND mc.lot_id=?",
            (wo_id, lot_id),
        ).fetchone()["s"]
        return row["issued"] - row["returned"] - row["scrapped"] - consumed

    def return_material(self, *, request_id: str, actor_id: str, wo_id: str, line_seq: int,
                        lot_id: str, qty: float, scan_ref: str) -> PilotReceipt:
        """工厂把未用完的料退回仓库（只能退真实在制量）。"""

        qty = _qty(qty, "qty")
        scan_ref = self._id(scan_ref, "scan_ref")
        payload = {"actor_id": actor_id, "wo_id": wo_id, "line_seq": line_seq,
                   "lot_id": lot_id, "qty": qty, "scan_ref": scan_ref}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_WAREHOUSE, ROLE_FACTORY)
            _replay = self._replay_receipt(conn, request_id, "return_material", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            lot_id = self._id(lot_id, "lot_id")
            line_seq = int(line_seq)
            self._get(conn, "work_orders", "wo_id", wo_id, "工单")
            self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")
            if self._scan_already_used(conn, scan_ref, request_id):
                raise ConflictError("该扫码已经处理，不能重复退料")
            wip = self._wip_qty(conn, wo_id, lot_id)
            if wip + QTY_EPS < qty:
                raise ConflictError(f"该工单此批次在制量仅 {wip}，不能退 {qty}")

            def create():
                txn_id = self._new_id()
                conn.execute(
                    "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,qty_delta,"
                    "ref_type,ref_id,scan_ref,created_by,created_at) "
                    "VALUES(?,?,?,?, 'return', ?,?,?,?,?,?)",
                    (txn_id, wo_id, line_seq, lot_id, qty, "work_order", wo_id,
                     scan_ref, actor_id, self._now()),
                )
                conn.execute("UPDATE material_lots SET remaining_qty=remaining_qty+? WHERE lot_id=?",
                             (qty, lot_id))
                conn.execute(
                    "UPDATE kit_allocations SET allocated_qty=MAX(allocated_qty-?,0) "
                    "WHERE wo_id=? AND line_seq=? AND lot_id=?",
                    (qty, wo_id, line_seq, lot_id),
                )
                self._assert_lot_balance(conn, lot_id)
                self._audit(conn, actor_id, "pilot.material.returned", "lot", lot_id,
                            {"wo_id": wo_id, "line_seq": line_seq, "qty": qty, "scan_ref": scan_ref})
                return "material_transaction", txn_id, {"txn_id": txn_id}

            return self._idempotent(conn, request_id=request_id, action="return_material",
                                    payload=payload, create=create)

    def report_wip_scrap(self, *, request_id: str, actor_id: str, wo_id: str, lot_id: str,
                         qty: float, note: str, scan_ref: str | None = None) -> PilotReceipt:
        """工厂先领料后补报损耗：在制料报废，不动仓库库存但参与守恒。"""

        qty = _qty(qty, "qty")
        note = self._text(note, "note")
        scan_ref = self._id(scan_ref, "scan_ref") if scan_ref else None
        payload = {"actor_id": actor_id, "wo_id": wo_id, "lot_id": lot_id, "qty": qty,
                   "note": note, "scan_ref": scan_ref}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_FACTORY)
            _replay = self._replay_receipt(conn, request_id, "report_wip_scrap", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            lot_id = self._id(lot_id, "lot_id")
            self._get(conn, "work_orders", "wo_id", wo_id, "工单")
            self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")
            if scan_ref and self._scan_already_used(conn, scan_ref, request_id):
                raise ConflictError("该扫码已经处理，不能重复报废")
            wip = self._wip_qty(conn, wo_id, lot_id)
            if wip + QTY_EPS < qty:
                raise ConflictError(f"在制量仅 {wip}，不能报废 {qty}")

            def create():
                txn_id = self._new_id()
                conn.execute(
                    "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,qty_delta,"
                    "ref_type,ref_id,scan_ref,created_by,created_at) "
                    "VALUES(?,?,?,?, 'wip_scrap', ?,?,?,?,?,?)",
                    (txn_id, wo_id, None, lot_id, -qty, "scrap_note", note,
                     scan_ref, actor_id, self._now()),
                )
                self._audit(conn, actor_id, "pilot.material.wip_scrapped", "lot", lot_id,
                            {"wo_id": wo_id, "qty": qty, "note": note, "scan_ref": scan_ref})
                return "material_transaction", txn_id, {"txn_id": txn_id, "wip_remaining": wip - qty}

            return self._idempotent(conn, request_id=request_id, action="report_wip_scrap",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 工序产出 / 组件序列 / 返工
    # ------------------------------------------------------------------

    def report_output(self, *, request_id: str, actor_id: str, wo_id: str, step_seq: int,
                      qty_good: float, qty_scrap: float, qty_rework: float,
                      materials: list[dict[str, Any]] | None = None,
                      input_components: list[str] | None = None,
                      assemblies: list[list[str]] | None = None,
                      material_usage: list[dict[str, int | str]] | None = None,
                      serials: list[str] | None = None) -> PilotReceipt:
        """上报一次工序产出并立即做数量守恒。

        qty_total = qty_good + qty_scrap + qty_rework（组件按件计数，必须为整数）。
        第 1 工序按配方行核销批量物料（用量 = qty_per × qty_total），可分拆到多个
        真实发料批次；后续工序每个产出单位通过 assemblies 精确声明其投入组件
        （多对一装配），未给 assemblies 时按顺序 1:1 对应 input_components。
        """

        qty_good = _nonneg(qty_good, "qty_good")
        qty_scrap = _nonneg(qty_scrap, "qty_scrap")
        qty_rework = _nonneg(qty_rework, "qty_rework")
        for value, field in ((qty_good, "qty_good"), (qty_scrap, "qty_scrap"),
                             (qty_rework, "qty_rework")):
            if not math.isclose(value, round(value), abs_tol=QTY_EPS):
                raise ValidationError(f"{field} 必须是整数件")
        qty_total = qty_good + qty_scrap + qty_rework
        if qty_total <= 0:
            raise ValidationError("产出总量必须大于 0")
        materials = materials or []
        grouped: dict[int, dict[str, float]] = {}
        raw_materials = []
        for item in materials:
            line_seq = int(item["line_seq"])
            lot_id = self._id(item.get("lot_id", ""), "lot_id")
            qty = _qty(item.get("qty"), "materials.qty")
            raw_materials.append({"line_seq": line_seq, "lot_id": lot_id, "qty": qty})
            entry = grouped.setdefault(line_seq, {})
            entry[lot_id] = entry.get(lot_id, 0.0) + qty
        # 装配关系：每个产出单位（先合格后返工）对应一组投入组件
        mappings: list[list[str]] | None = None
        if assemblies is not None:
            mappings = [[self._id(c, "assembly_component") for c in group]
                        for group in assemblies]
        elif input_components:
            mappings = [[self._id(c, "input_component")] for c in input_components]
        if mappings is not None:
            if len(mappings) != int(qty_total):
                raise ValidationError("装配关系数量必须等于产出总量")
            if any(not group for group in mappings):
                raise ValidationError("每个产出单位至少要有一个投入组件")
        flat_inputs = sorted({c for group in (mappings or []) for c in group})
        # 组件级用料归属：同一配方行由多个批次（含替代料）供料时，
        # 必须逐件声明每件产出来自哪个批次；单批次则自动归属。
        usage_norm: list[dict[int, str]] | None = None
        if material_usage is not None:
            usage_norm = []
            for entry in material_usage:
                if not isinstance(entry, dict):
                    raise ValidationError("material_usage 每项必须是 {line_seq: lot_id}")
                usage_norm.append({int(k): self._id(v, "usage.lot_id") for k, v in entry.items()})
        payload = {"actor_id": actor_id, "wo_id": wo_id, "step_seq": step_seq,
                   "qty_good": qty_good, "qty_scrap": qty_scrap, "qty_rework": qty_rework,
                   "materials": materials, "mappings": mappings,
                   "material_usage": usage_norm, "serials": serials}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_FACTORY)
            _replay = self._replay_receipt(conn, request_id, "report_output", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            step_seq = int(step_seq)
            wo = self._get(conn, "work_orders", "wo_id", wo_id, "工单")
            if wo["status"] in ("completed", "closed", "frozen"):
                raise ConflictError(f"工单状态 {wo['status']}，不能上报产出")
            if step_seq < 1:
                raise ValidationError("step_seq 必须 >= 1")
            # 物料核销：该工序全部物料配方行必须按 qty_per × qty_total 足额核销，
            # 且只能使用本工单真实领用、仍在制的批次
            step_material_lines = conn.execute(
                "SELECT * FROM recipe_lines WHERE recipe_id=? AND step_seq=? AND input_kind='material'",
                (wo["recipe_id"], step_seq),
            ).fetchall()
            for line in step_material_lines:
                lot_map = grouped.get(line["line_seq"])
                expected = line["qty_per"] * qty_total
                if lot_map is None:
                    raise ConflictError(f"配方行 {line['line_seq']} 未核销物料，缺料生产")
                actual = sum(lot_map.values())
                if not math.isclose(actual, expected, abs_tol=QTY_EPS):
                    raise ConflictError(
                        f"配方行 {line['line_seq']} 应投 {expected}，实投 {actual}")
            for line_seq, lot_map in grouped.items():
                line = self._recipe_line(conn, wo, line_seq)
                if line["step_seq"] != step_seq or line["input_kind"] != "material":
                    raise ValidationError(f"配方行 {line_seq} 不属于该工序的物料行")
                for lot_id, qty in lot_map.items():
                    wip = self._wip_qty(conn, wo_id, lot_id)
                    if wip + QTY_EPS < qty:
                        raise ConflictError(
                            f"批次 {lot_id} 在制量 {wip} 不足以核销 {qty}")
            # 多批次供料行必须有逐件用料归属
            multi_lines = {ls for ls, lm in grouped.items() if len(lm) > 1}
            if serials is not None and len(serials) != int(qty_good):
                raise ValidationError("serials 数量必须等于合格品数量")
            if usage_norm is None:
                if multi_lines:
                    raise ConflictError(
                        f"配方行 {sorted(multi_lines)} 由多个批次供料，必须提供 material_usage 逐件归属")
            else:
                if len(usage_norm) != int(qty_total):
                    raise ValidationError("material_usage 条数必须等于产出总量（合格+报废+返工）")
                for index, entry in enumerate(usage_norm):
                    unknown = set(entry) - {ls for ls in grouped}
                    if unknown:
                        raise ValidationError(f"第 {index + 1} 件用料归属引用了未核销的配方行 {sorted(unknown)}")
                    for line_seq, lot_id in entry.items():
                        if lot_id not in grouped[line_seq]:
                            raise ConflictError(
                                f"第 {index + 1} 件归属批次 {lot_id} 不在配方行 {line_seq} 的核销批次中")
                for line_seq in multi_lines:
                    declared = sum(1 for entry in usage_norm if line_seq in entry)
                    if declared != int(qty_total):
                        raise ConflictError(
                            f"配方行 {line_seq} 的每件产出都必须在 material_usage 中归属一个批次")
                    line = self._recipe_line(conn, wo, line_seq)
                    counts: dict[str, int] = {}
                    for entry in usage_norm:
                        lot_id = entry.get(line_seq)
                        if lot_id is not None:
                            counts[lot_id] = counts.get(lot_id, 0) + 1
                    for lot_id, count in counts.items():
                        if not math.isclose(count * line["qty_per"],
                                            grouped[line_seq][lot_id], abs_tol=QTY_EPS):
                            raise ConflictError(
                                f"配方行 {line_seq} 批次 {lot_id} 逐件归属量 "
                                f"{count * line['qty_per']} 与核销总量 "
                                f"{grouped[line_seq][lot_id]} 不一致")
            if step_seq > 1 and mappings is None:
                raise ConflictError("后续工序必须通过 input_components 或 assemblies 声明投入组件")
            if step_seq == 1 and mappings is not None:
                raise ConflictError("第一道工序不接受组件投入")
            for component_id in flat_inputs:
                comp = self._get(conn, "components", "component_id", component_id, "组件")
                if comp["wo_id"] != wo_id or comp["step_seq"] != step_seq - 1:
                    raise ConflictError(f"组件 {component_id} 不属于该工单的上一工序")
                if comp["status"] != "released" or comp["frozen"]:
                    raise ConflictError("只有未冻结且已双人放行的组件才能进入下一工序")
            # 防止同一组件被重复装配
            if flat_inputs and conn.execute(
                    "SELECT 1 FROM component_inputs WHERE input_component_id IN ({}) LIMIT 1".format(
                        ",".join("?" * len(flat_inputs))),
                    flat_inputs).fetchone():
                raise ConflictError("组件已被其他产出消耗")

            def create():
                output_id = self._new_id()
                conn.execute(
                    "INSERT INTO process_outputs(output_id,wo_id,step_seq,kind,qty_total,"
                    "qty_good,qty_scrap,qty_rework,produced_by,created_at) "
                    "VALUES(?,?,?, 'normal', ?,?,?,?,?,?)",
                    (output_id, wo_id, step_seq, qty_total, qty_good, qty_scrap,
                     qty_rework, actor_id, self._now()),
                )
                aggregated: dict[tuple[int, str], float] = {}
                for item in raw_materials:
                    key = (item["line_seq"], item["lot_id"])
                    aggregated[key] = aggregated.get(key, 0.0) + item["qty"]
                for (line_seq, lot_id), qty in aggregated.items():
                    conn.execute(
                        "INSERT INTO material_consumption(output_id,line_seq,lot_id,qty) "
                        "VALUES(?,?,?,?)",
                        (output_id, line_seq, lot_id, qty),
                    )
                    conn.execute(
                        "INSERT INTO material_transactions(txn_id,wo_id,line_seq,lot_id,kind,"
                        "qty_delta,ref_type,ref_id,scan_ref,created_by,created_at) "
                        "VALUES(?,?,?,?, 'consume', ?,?,?,?,?,?)",
                        (self._new_id(), wo_id, line_seq, lot_id, -qty,
                         "process_output", output_id, None, actor_id, self._now()),
                    )
                    self._assert_lot_balance(conn, lot_id)
                good_ids, rework_ids, scrap_ids = [], [], []
                for index in range(int(qty_total)):
                    component_id = self._new_id()
                    if index < qty_good:
                        serial = serials[index] if serials is not None else \
                            f"{wo_id}-S{step_seq}-{output_id[:8]}-{index + 1:04d}"
                        status = "pending_inspection"
                    elif index < qty_good + qty_scrap:
                        scrap_index = index - int(qty_good) + 1
                        serial = f"{wo_id}-S{step_seq}-{output_id[:8]}-SC{scrap_index:03d}"
                        status = "scrapped"
                    else:
                        rw_index = index - int(qty_good) - int(qty_scrap) + 1
                        serial = f"{wo_id}-S{step_seq}-{output_id[:8]}-RW{rw_index:03d}"
                        status = "blocked"
                    try:
                        conn.execute(
                            "INSERT INTO components(component_id,product_code,output_id,wo_id,"
                            "step_seq,serial_no,status,frozen,created_at) VALUES(?,?,?,?,?,?,?,0,?)",
                            (component_id, wo["product_code"], output_id, wo_id, step_seq,
                             serial, status, self._now()),
                        )
                    except Exception as exc:
                        raise ConflictError(f"组件序列号 {serial} 重复") from exc
                    if status == "pending_inspection":
                        good_ids.append(component_id)
                    elif status == "scrapped":
                        scrap_ids.append(component_id)
                    else:
                        conn.execute(
                            "INSERT INTO rework_jobs(rework_id,wo_id,step_seq,source_component_id,"
                            "result_component_id,note,created_by,created_at,finished_at) "
                            "VALUES(?,?,?,?,NULL, '工序判返工', ?,?,NULL)",
                            (self._new_id(), wo_id, step_seq, component_id,
                             actor_id, self._now()),
                        )
                        rework_ids.append(component_id)
                produced_ids = good_ids + scrap_ids + rework_ids
                # 组件级用料归属：多批次按 material_usage，单批次自动归属
                auto_usage = {ls: next(iter(lot_map)) for ls, lot_map in grouped.items()
                              if len(lot_map) == 1}
                for unit_index, component_id in enumerate(produced_ids):
                    entry = usage_norm[unit_index] if usage_norm is not None else {}
                    for line_seq, lot_map in grouped.items():
                        lot_id = entry.get(line_seq, auto_usage.get(line_seq))
                        if lot_id is None:
                            continue
                        line = self._recipe_line(conn, wo, line_seq)
                        conn.execute(
                            "INSERT INTO output_component_lots(component_id,line_seq,lot_id,qty) "
                            "VALUES(?,?,?,?)",
                            (component_id, line_seq, lot_id, line["qty_per"]),
                        )
                # 产出顺序与 mappings 一致：合格件 → 报废件 → 返工件
                for parent_id, group in zip(produced_ids, mappings or []):
                    for child in group:
                        conn.execute(
                            "INSERT INTO component_inputs(component_id,input_component_id,step_seq)"
                            " VALUES(?,?,?)",
                            (parent_id, child, step_seq),
                        )
                for child in flat_inputs:
                    conn.execute("UPDATE components SET status='consumed' WHERE component_id=?",
                                 (child,))
                conn.execute("UPDATE work_orders SET status='in_progress' WHERE wo_id=? AND status='open'",
                             (wo_id,))
                self._audit(conn, actor_id, "pilot.output.reported", "process_output", output_id,
                            {"wo_id": wo_id, "step_seq": step_seq, "qty_total": qty_total,
                             "qty_good": qty_good, "qty_scrap": qty_scrap,
                             "qty_rework": qty_rework, "components": good_ids,
                             "scrap_components": scrap_ids,
                             "rework_components": rework_ids,
                             "materials": materials, "assembly": bool(mappings)})
                return "process_output", output_id, {
                    "output_id": output_id, "qty_total": qty_total,
                    "components": good_ids, "scrap_components": scrap_ids,
                    "rework_components": rework_ids}

            return self._idempotent(conn, request_id=request_id, action="report_output",
                                    payload=payload, create=create)

    def open_rework(self, *, request_id: str, actor_id: str, component_id: str,
                    note: str) -> PilotReceipt:
        """对检验不合格/被阻断的组件立案返工（工序判返工的件已有任务，不重复立案）。"""

        note = self._text(note, "note")
        payload = {"actor_id": actor_id, "component_id": component_id, "note": note}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_FACTORY)
            _replay = self._replay_receipt(conn, request_id, "open_rework", payload)
            if _replay is not None:
                return _replay
            component_id = self._id(component_id, "component_id")
            comp = self._get(conn, "components", "component_id", component_id, "组件")
            if comp["frozen"]:
                raise ConflictError("组件已冻结，不能立案返工")
            if comp["status"] not in ("blocked",):
                raise ConflictError(f"组件状态 {comp['status']} 不能立案返工")
            existing = conn.execute(
                "SELECT rework_id FROM rework_jobs WHERE source_component_id=? AND finished_at IS NULL",
                (component_id,),
            ).fetchone()
            if existing:
                raise ConflictError("该组件已有未完成的返工任务")

            def create():
                rework_id = self._new_id()
                conn.execute(
                    "INSERT INTO rework_jobs(rework_id,wo_id,step_seq,source_component_id,"
                    "result_component_id,note,created_by,created_at,finished_at) "
                    "VALUES(?,?,?,?,NULL,?,?,?,NULL)",
                    (rework_id, comp["wo_id"], comp["step_seq"], component_id,
                     note, actor_id, self._now()),
                )
                self._audit(conn, actor_id, "pilot.rework.opened", "rework_job", rework_id,
                            {"component_id": component_id, "note": note})
                return "rework_job", rework_id, {"rework_id": rework_id}

            return self._idempotent(conn, request_id=request_id, action="open_rework",
                                    payload=payload, create=create)

    def finish_rework(self, *, request_id: str, actor_id: str, rework_id: str,
                      decision: str, note: str = "", new_serial: str | None = None) -> PilotReceipt:
        """处置返工组件：原位修复 / 报废 / 重建。

        无论哪种方式都保留原始谱系：原位修复保留同一组件 ID 与全部投入关系；
        重建产生新组件并写入 reworked_from 关系，复制原组件的投入谱系。
        """

        note = str(note or "").strip()
        if decision not in ("repaired", "scrapped", "rebuilt"):
            raise ValidationError("decision 必须是 repaired/scrapped/rebuilt")
        payload = {"actor_id": actor_id, "rework_id": rework_id, "decision": decision,
                   "note": note, "new_serial": new_serial}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_FACTORY)
            _replay = self._replay_receipt(conn, request_id, "finish_rework", payload)
            if _replay is not None:
                return _replay
            rework_id = self._id(rework_id, "rework_id")
            job = self._get(conn, "rework_jobs", "rework_id", rework_id, "返工任务")
            if job["finished_at"]:
                raise ConflictError("返工任务已完成")
            source = self._get(conn, "components", "component_id",
                               job["source_component_id"], "组件")
            if source["frozen"]:
                raise ConflictError("组件已冻结，不能返工")

            def create():
                result_id = source["component_id"]
                if decision == "scrapped":
                    conn.execute(
                        "UPDATE components SET status='scrapped' WHERE component_id=?",
                        (source["component_id"],))
                elif decision == "repaired":
                    conn.execute(
                        "UPDATE components SET status='pending_inspection' WHERE component_id=?",
                        (source["component_id"],))
                    conn.execute(
                        "UPDATE rework_jobs SET result_component_id=? WHERE rework_id=?",
                        (source["component_id"], rework_id))
                else:
                    new_id = self._new_id()
                    serial = self._text(new_serial or f"{source['serial_no']}-RB", "new_serial", 160)
                    # 重建件计入一条 kind='rework' 的工序产出，保持逐件守恒
                    rework_output = self._new_id()
                    conn.execute(
                        "INSERT INTO process_outputs(output_id,wo_id,step_seq,kind,qty_total,"
                        "qty_good,qty_scrap,qty_rework,produced_by,created_at) "
                        "VALUES(?,?,?, 'rework', 1,1,0,0,?,?)",
                        (rework_output, source["wo_id"], source["step_seq"],
                         actor_id, self._now()),
                    )
                    conn.execute(
                        "INSERT INTO components(component_id,product_code,output_id,wo_id,"
                        "step_seq,serial_no,status,frozen,created_at) "
                        "VALUES(?,?,?,?,?,?,'pending_inspection',0,?)",
                        (new_id, source["product_code"], rework_output, source["wo_id"],
                         source["step_seq"], serial, self._now()),
                    )
                    conn.execute(
                        "INSERT INTO component_relations(relation_id,parent_component_id,"
                        "child_component_id,relation,created_at) VALUES(?,?,?, 'reworked_from',?)",
                        (self._new_id(), new_id, source["component_id"], self._now()),
                    )
                    for row in conn.execute(
                            "SELECT input_component_id,step_seq FROM component_inputs WHERE component_id=?",
                            (source["component_id"],)):
                        conn.execute(
                            "INSERT OR IGNORE INTO component_inputs(component_id,input_component_id,step_seq)"
                            " VALUES(?,?,?)",
                            (new_id, row["input_component_id"], row["step_seq"]),
                        )
                    for row in conn.execute(
                            "SELECT line_seq,lot_id,qty FROM output_component_lots WHERE component_id=?",
                            (source["component_id"],)):
                        conn.execute(
                            "INSERT OR IGNORE INTO output_component_lots(component_id,line_seq,lot_id,qty)"
                            " VALUES(?,?,?,?)",
                            (new_id, row["line_seq"], row["lot_id"], row["qty"]),
                        )
                    conn.execute(
                        "UPDATE components SET status='reworked' WHERE component_id=?",
                        (source["component_id"],))
                    conn.execute(
                        "UPDATE rework_jobs SET result_component_id=? WHERE rework_id=?",
                        (new_id, rework_id))
                    result_id = new_id
                conn.execute("UPDATE rework_jobs SET finished_at=?, note=? WHERE rework_id=?",
                             (self._now(), note or job["note"], rework_id))
                self._audit(conn, actor_id, "pilot.rework.finished", "rework_job", rework_id,
                            {"source_component_id": source["component_id"],
                             "result_component_id": result_id, "decision": decision, "note": note})
                return "component", result_id, {"component_id": result_id, "decision": decision}

            return self._idempotent(conn, request_id=request_id, action="finish_rework",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 组件检验与双人放行
    # ------------------------------------------------------------------

    def inspect_component(self, *, request_id: str, actor_id: str, component_id: str,
                          spec_id: str, decision: str, findings: dict[str, Any] | None = None,
                          is_additional: bool = False) -> PilotReceipt:
        findings = findings or {}
        if decision not in ("pass", "fail"):
            raise ValidationError("decision 必须是 pass 或 fail")
        if not isinstance(findings, dict):
            raise ValidationError("findings 必须是对象")
        payload = {"actor_id": actor_id, "component_id": component_id, "spec_id": spec_id,
                   "decision": decision, "findings": findings, "is_additional": is_additional}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC)
            _replay = self._replay_receipt(conn, request_id, "inspect_component", payload)
            if _replay is not None:
                return _replay
            component_id = self._id(component_id, "component_id")
            spec_id = self._id(spec_id, "spec_id")
            comp = self._get(conn, "components", "component_id", component_id, "组件")
            wo = self._get(conn, "work_orders", "wo_id", comp["wo_id"], "工单")
            spec = self._get(conn, "inspection_specs", "spec_id", spec_id, "检验规范")
            if spec["applies_to"] != "component" or spec["ref_id"] != comp["product_code"]:
                raise ValidationError("检验规范与产品不匹配")
            if spec["version"] != wo["recipe_version"]:
                raise ConflictError(
                    f"检验规范版本 {spec['version']} 与工单配方版本 {wo['recipe_version']} 不一致")
            if is_additional:
                if comp["status"] not in ("released", "consumed", "blocked"):
                    raise ConflictError("只能对已放行或已流转的组件发起追加抽检")
            else:
                if comp["status"] != "pending_inspection":
                    raise ConflictError(
                        f"组件状态 {comp['status']} 不能做初次检验；已放行件请使用追加抽检")

            def create():
                result_id = self._new_id()
                conn.execute(
                    "INSERT INTO inspection_results(result_id,spec_id,target_kind,target_id,"
                    "is_additional,decision,inspector_id,findings_json,created_at) "
                    "VALUES(?,?, 'component', ?,?,?,?,?,?)",
                    (result_id, spec_id, component_id, 1 if is_additional else 0,
                     decision, actor_id, canonical_json(findings), self._now()),
                )
                if decision == "fail":
                    conn.execute(
                        "UPDATE components SET status='blocked' WHERE component_id=?",
                        (component_id,))
                    if is_additional:
                        self._apply_freeze(conn, actor_id=actor_id, source_kind="component",
                                           source_id=component_id, reason="inspection_fail",
                                           note=f"组件追加抽检不合格: {canonical_json(findings)[:200]}")
                self._audit(conn, actor_id, "pilot.component.inspected", "component",
                            component_id, {"spec_id": spec_id, "decision": decision,
                                           "is_additional": is_additional, "findings": findings})
                return "inspection_result", result_id, {"result_id": result_id, "decision": decision}

            return self._idempotent(conn, request_id=request_id, action="inspect_component",
                                    payload=payload, create=create)

    def request_release(self, *, request_id: str, actor_id: str, component_id: str) -> PilotReceipt:
        """为合格组件申请双人放行（质检 + 品牌）。"""

        payload = {"actor_id": actor_id, "component_id": component_id}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_FACTORY, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "request_release", payload)
            if _replay is not None:
                return _replay
            component_id = self._id(component_id, "component_id")
            comp = self._get(conn, "components", "component_id", component_id, "组件")
            if comp["status"] not in ("pending_inspection",):
                raise ConflictError(f"组件状态 {comp['status']} 不能申请放行")
            wo = self._get(conn, "work_orders", "wo_id", comp["wo_id"], "工单")
            result = conn.execute(
                "SELECT ir.* FROM inspection_results ir JOIN inspection_specs isp "
                "ON isp.spec_id=ir.spec_id "
                "WHERE ir.target_kind='component' AND ir.target_id=? AND ir.decision='pass' "
                "AND ir.is_additional=0 AND isp.version=? "
                "ORDER BY ir.created_at DESC LIMIT 1",
                (component_id, wo["recipe_version"]),
            ).fetchone()
            if result is None:
                raise ConflictError("组件缺少与配方版本匹配的合格检验记录")
            existing = conn.execute(
                "SELECT * FROM release_tickets WHERE component_id=? AND status='pending'",
                (component_id,),
            ).fetchone()
            if existing:
                raise ConflictError("该组件已有待审批放行单")

            def create():
                ticket_id = self._new_id()
                conn.execute(
                    "INSERT INTO release_tickets(ticket_id,component_id,recipe_id,recipe_version,"
                    "inspection_result_id,status,requested_by,created_at,released_at) "
                    "VALUES(?,?,?,?,?, 'pending', ?,?,NULL)",
                    (ticket_id, component_id, wo["recipe_id"], wo["recipe_version"],
                     result["result_id"], actor_id, self._now()),
                )
                self._audit(conn, actor_id, "pilot.release.requested", "release_ticket",
                            ticket_id, {"component_id": component_id})
                return "release_ticket", ticket_id, {"ticket_id": ticket_id, "status": "pending"}

            return self._idempotent(conn, request_id=request_id, action="request_release",
                                    payload=payload, create=create)

    def approve_release(self, *, request_id: str, actor_id: str, ticket_id: str,
                        role: str) -> PilotReceipt:
        """质检与品牌方双人批准；两人必须是不同操作者，齐备后组件方可流转。"""

        if role not in (ROLE_QC, ROLE_BRAND):
            raise ValidationError("role 必须是 qinspector 或 brand")
        payload = {"actor_id": actor_id, "ticket_id": ticket_id, "role": role}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "approve_release", payload)
            if _replay is not None:
                return _replay
            if actor.role != "admin" and actor.role != role:
                raise PermissionDenied("只能以本人所属角色批准")
            ticket_id = self._id(ticket_id, "ticket_id")
            ticket = self._get(conn, "release_tickets", "ticket_id", ticket_id, "放行单")
            if ticket["status"] != "pending":
                raise ConflictError("放行单不在待审批状态")
            comp = self._get(conn, "components", "component_id",
                             ticket["component_id"], "组件")
            if comp["frozen"]:
                raise ConflictError("组件已冻结，不能放行")
            other = conn.execute(
                "SELECT actor_id FROM release_approvals WHERE ticket_id=? AND role<>?",
                (ticket_id, role),
            ).fetchone()
            if other and other["actor_id"] == actor_id:
                raise ConflictError("双人放行必须由两名不同操作者完成")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO release_approvals(ticket_id,role,actor_id,decision,created_at)"
                        " VALUES(?,?,?, 'approve',?)",
                        (ticket_id, role, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该角色已经批准过此放行单") from exc
                approvals = conn.execute(
                    "SELECT COUNT(*) AS c FROM release_approvals WHERE ticket_id=? AND decision='approve'",
                    (ticket_id,),
                ).fetchone()["c"]
                released = approvals >= 2
                if released:
                    conn.execute(
                        "UPDATE release_tickets SET status='released', released_at=? WHERE ticket_id=?",
                        (self._now(), ticket_id),
                    )
                    conn.execute(
                        "UPDATE components SET status='released' WHERE component_id=?",
                        (comp["component_id"],))
                self._audit(conn, actor_id, "pilot.release.approved", "release_ticket",
                            ticket_id, {"role": role, "released": released})
                return "release_ticket", ticket_id, {"ticket_id": ticket_id,
                                                     "status": "released" if released else "pending"}

            return self._idempotent(conn, request_id=request_id, action="approve_release",
                                    payload=payload, create=create)

    def reject_release(self, *, request_id: str, actor_id: str, ticket_id: str,
                       reason: str) -> PilotReceipt:
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "ticket_id": ticket_id, "reason": reason}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "reject_release", payload)
            if _replay is not None:
                return _replay
            ticket_id = self._id(ticket_id, "ticket_id")
            ticket = self._get(conn, "release_tickets", "ticket_id", ticket_id, "放行单")
            if ticket["status"] != "pending":
                raise ConflictError("放行单不在待审批状态")

            def create():
                conn.execute(
                    "UPDATE release_tickets SET status='rejected' WHERE ticket_id=?",
                    (ticket_id,))
                conn.execute(
                    "UPDATE components SET status='blocked' WHERE component_id=?",
                    (ticket["component_id"],))
                self._audit(conn, actor_id, "pilot.release.rejected", "release_ticket",
                            ticket_id, {"reason": reason})
                return "release_ticket", ticket_id, {"ticket_id": ticket_id, "status": "rejected"}

            return self._idempotent(conn, request_id=request_id, action="reject_release",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 订单 / 渠道批次 / 包装组合
    # ------------------------------------------------------------------

    def register_order(self, *, request_id: str, actor_id: str, order_id: str,
                       channel: str, product_code: str, qty: float) -> PilotReceipt:
        qty = _qty(qty, "qty")
        payload = {"actor_id": actor_id, "order_id": order_id, "channel": channel,
                   "product_code": product_code, "qty": qty}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_order", payload)
            if _replay is not None:
                return _replay
            order_id = self._id(order_id, "order_id")
            channel = self._text(channel, "channel", 120)
            product_code = self._id(product_code, "product_code")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO orders(order_id,channel,product_code,qty,status,created_by,created_at)"
                        " VALUES(?,?,?,?,'open',?,?)",
                        (order_id, channel, product_code, qty, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("订单号已存在") from exc
                self._audit(conn, actor_id, "pilot.order.registered", "order", order_id,
                            {"channel": channel, "product_code": product_code, "qty": qty})
                return "order", order_id, {"order_id": order_id}

            return self._idempotent(conn, request_id=request_id, action="register_order",
                                    payload=payload, create=create)

    def register_channel_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                               order_id: str, qty_planned: float) -> PilotReceipt:
        qty_planned = _qty(qty_planned, "qty_planned")
        payload = {"actor_id": actor_id, "batch_id": batch_id, "order_id": order_id,
                   "qty_planned": qty_planned}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "register_channel_batch", payload)
            if _replay is not None:
                return _replay
            batch_id = self._id(batch_id, "batch_id")
            order_id = self._id(order_id, "order_id")
            self._get(conn, "orders", "order_id", order_id, "订单")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO channel_batches(batch_id,order_id,qty_planned,status,"
                        "created_by,created_at,sealed_at) VALUES(?,?,?, 'open', ?,?,NULL)",
                        (batch_id, order_id, qty_planned, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("渠道批次号已存在") from exc
                self._audit(conn, actor_id, "pilot.channel_batch.registered", "channel_batch",
                            batch_id, {"order_id": order_id, "qty_planned": qty_planned})
                return "channel_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="register_channel_batch", payload=payload, create=create)

    def pack_package(self, *, request_id: str, actor_id: str, channel_batch_id: str,
                     component_ids: list[str]) -> PilotReceipt:
        """把已放行的最终组件装入渠道包装；一个组件只能进入一个包装。"""

        component_ids = [self._id(c, "component_id") for c in component_ids]
        if not component_ids:
            raise ValidationError("component_ids 不能为空")
        payload = {"actor_id": actor_id, "channel_batch_id": channel_batch_id,
                   "component_ids": component_ids}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_BRAND, ROLE_WAREHOUSE)
            _replay = self._replay_receipt(conn, request_id, "pack_package", payload)
            if _replay is not None:
                return _replay
            channel_batch_id = self._id(channel_batch_id, "channel_batch_id")
            cb = self._get(conn, "channel_batches", "batch_id", channel_batch_id, "渠道批次")
            if cb["status"] == "frozen":
                raise ConflictError("渠道批次已冻结")
            order = self._get(conn, "orders", "order_id", cb["order_id"], "订单")
            for component_id in component_ids:
                comp = self._get(conn, "components", "component_id", component_id, "组件")
                if comp["status"] != "released" or comp["frozen"]:
                    raise ConflictError(f"组件 {component_id} 未放行或已冻结，不能包装")
                if comp["product_code"] != order["product_code"]:
                    raise ConflictError(f"组件 {component_id} 不属于订单产品")
                if conn.execute("SELECT 1 FROM package_items WHERE component_id=?",
                                (component_id,)).fetchone():
                    raise ConflictError(f"组件 {component_id} 已在其他包装中")

            def create():
                package_id = self._new_id()
                conn.execute(
                    "INSERT INTO packages(package_id,channel_batch_id,wo_id,status,created_by,created_at)"
                    " VALUES(?,?,?, 'sealed', ?,?)",
                    (package_id, channel_batch_id, None, actor_id, self._now()),
                )
                for component_id in component_ids:
                    conn.execute(
                        "INSERT INTO package_items(package_id,component_id) VALUES(?,?)",
                        (package_id, component_id),
                    )
                conn.execute("UPDATE packages SET wo_id=(SELECT wo_id FROM components WHERE component_id=?) WHERE package_id=?",
                             (component_ids[0], package_id))
                self._audit(conn, actor_id, "pilot.package.sealed", "package", package_id,
                            {"channel_batch_id": channel_batch_id,
                             "component_ids": component_ids})
                return "package", package_id, {"package_id": package_id,
                                               "component_count": len(component_ids)}

            return self._idempotent(conn, request_id=request_id, action="pack_package",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 守恒校验
    # ------------------------------------------------------------------

    def close_work_order(self, *, request_id: str, actor_id: str, wo_id: str) -> PilotReceipt:
        """结案工单：守恒必须成立，且不能残留未处理的在制料/待返工组件。"""

        payload = {"actor_id": actor_id, "wo_id": wo_id}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_FACTORY, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "close_work_order", payload)
            if _replay is not None:
                return _replay
            wo_id = self._id(wo_id, "wo_id")
            wo = self._get(conn, "work_orders", "wo_id", wo_id, "工单")
            if wo["status"] in ("closed",):
                raise ConflictError("工单已结案")
            if wo["status"] == "frozen":
                raise ConflictError("工单处于冻结状态，不能结案")
            # 守恒校验必须在同一写事务内完成
            balance = self.verify_work_order_balance(wo_id)
            if not balance["balanced"]:
                raise ConflictError("工单数量不守恒，不能结案")
            open_wip = 0.0
            for row in conn.execute(
                    "SELECT DISTINCT lot_id FROM material_transactions WHERE wo_id=?", (wo_id,)):
                open_wip += max(self._wip_qty(conn, wo_id, row["lot_id"]), 0.0)
            if open_wip > QTY_EPS:
                raise ConflictError(f"工单仍有 {open_wip} 在制料未消耗或退回，不能结案")
            open_rework = conn.execute(
                "SELECT COUNT(*) AS c FROM rework_jobs WHERE wo_id=? AND finished_at IS NULL",
                (wo_id,),
            ).fetchone()["c"]
            if open_rework:
                raise ConflictError(f"仍有 {open_rework} 个未完成返工任务，不能结案")

            def do_close():
                conn.execute(
                    "UPDATE work_orders SET status='closed', closed_at=? WHERE wo_id=?",
                    (self._now(), wo_id),
                )
                self._audit(conn, actor_id, "pilot.wo.closed", "work_order", wo_id,
                            {"balance": balance})
                return "work_order", wo_id, {"wo_id": wo_id, "status": "closed"}

            return self._idempotent(conn, request_id=request_id, action="close_work_order",
                                    payload=payload, create=do_close)

    def _assert_lot_balance(self, conn, lot_id: str) -> None:
        # 仓库账只受收货/发料/退料/重组/库存报废影响；
        # consume 与 wip_scrap 是工单在制流转，不改变仓库余量。
        row = conn.execute(
            "SELECT COALESCE(SUM(qty_delta),0) AS s FROM material_transactions "
            "WHERE lot_id=? AND kind IN ('receive','issue','return','scrap','repack_out','repack_in')",
            (lot_id,),
        ).fetchone()
        lot = conn.execute("SELECT remaining_qty,qty_received FROM material_lots WHERE lot_id=?",
                           (lot_id,)).fetchone()
        if not math.isclose(row["s"], lot["remaining_qty"], abs_tol=QTY_EPS):
            raise ConflictError(
                f"库存批次 {lot_id} 数量不守恒：账面 {lot['remaining_qty']}，流水 {row['s']}")
        if lot["remaining_qty"] < -QTY_EPS:
            raise ConflictError(f"库存批次 {lot_id} 出现负库存")

    def verify_lot_balance(self, lot_id: str) -> StockSnapshot:
        lot_id = self._id(lot_id, "lot_id")
        conn = self.database.connection
        lot = self._get(conn, "material_lots", "lot_id", lot_id, "库存批次")
        row = conn.execute(
            "SELECT "
            " COALESCE(SUM(CASE WHEN kind='issue' THEN -qty_delta END),0) AS issued, "
            " COALESCE(SUM(CASE WHEN kind='return' THEN qty_delta END),0) AS returned, "
            " COALESCE(SUM(CASE WHEN kind='wip_scrap' THEN -qty_delta END),0) AS wip_scrapped, "
            " COALESCE(SUM(CASE WHEN kind='consume' THEN -qty_delta END),0) AS consumed, "
            " COALESCE(SUM(CASE WHEN kind='scrap' THEN -qty_delta END),0) AS stock_scrapped, "
            " COALESCE(SUM(CASE WHEN kind IN ('receive','issue','return','scrap','repack_out','repack_in') "
            "                  THEN qty_delta END),0) AS ledger "
            "FROM material_transactions WHERE lot_id=?",
            (lot_id,),
        ).fetchone()
        input_qty = row["issued"] - row["returned"]
        wip_qty = input_qty - row["wip_scrapped"] - row["consumed"]
        balanced = (
            math.isclose(row["ledger"], lot["remaining_qty"], abs_tol=QTY_EPS)
            and wip_qty >= -QTY_EPS
            and math.isclose(
                input_qty, row["consumed"] + row["wip_scrapped"] + wip_qty, abs_tol=QTY_EPS)
        )
        return StockSnapshot("lot", lot_id, input_qty, row["consumed"],
                             row["wip_scrapped"] + row["stock_scrapped"], 0.0,
                             max(wip_qty, 0.0), balanced)

    def verify_work_order_balance(self, wo_id: str) -> dict[str, Any]:
        """核对工单全部数量：投入=合格+报废+返工/在制，组件逐工序不丢失。"""

        wo_id = self._id(wo_id, "wo_id")
        conn = self.database.connection
        wo = self._get(conn, "work_orders", "wo_id", wo_id, "工单")
        # 1) 物料：投入 = 消耗 + 在制报废 + 在制余量
        lots = {}
        for row in conn.execute(
                "SELECT lot_id, kind, qty_delta FROM material_transactions "
                "WHERE wo_id IS NOT NULL AND wo_id=?", (wo_id,)):
            entry = lots.setdefault(row["lot_id"], {"issued": 0.0, "returned": 0.0,
                                                    "wip_scrap": 0.0, "consumed": 0.0})
            if row["kind"] == "issue":
                entry["issued"] += -row["qty_delta"]
            elif row["kind"] == "return":
                entry["returned"] += row["qty_delta"]
            elif row["kind"] == "wip_scrap":
                entry["wip_scrap"] += -row["qty_delta"]
            elif row["kind"] == "consume":
                entry["consumed"] += -row["qty_delta"]
        material_balanced = True
        lot_snapshots = []
        for lot_id, e in lots.items():
            wip = e["issued"] - e["returned"] - e["wip_scrap"] - e["consumed"]
            ok = math.isclose(e["issued"] - e["returned"],
                              e["consumed"] + e["wip_scrap"] + wip, abs_tol=QTY_EPS) \
                and wip >= -QTY_EPS
            material_balanced &= ok
            lot_snapshots.append({"lot_id": lot_id, "input": e["issued"] - e["returned"],
                                  "consumed": e["consumed"], "scrap": e["wip_scrap"],
                                  "wip": max(wip, 0.0), "balanced": ok})
        # 2) 逐工序：总产出 = 合格 + 报废 + 返工；跨工序组件数守恒
        steps = {}
        for row in conn.execute("SELECT * FROM process_outputs WHERE wo_id=?", (wo_id,)):
            s = steps.setdefault(row["step_seq"], {"total": 0.0, "good": 0.0,
                                                   "scrap": 0.0, "rework": 0.0})
            s["total"] += row["qty_total"]
            s["good"] += row["qty_good"]
            s["scrap"] += row["qty_scrap"]
            s["rework"] += row["qty_rework"]
        step_balanced = True
        step_snapshots = []
        for step_seq in sorted(steps):
            s = steps[step_seq]
            ok = math.isclose(s["total"], s["good"] + s["scrap"] + s["rework"], abs_tol=QTY_EPS)
            produced = conn.execute(
                "SELECT COUNT(*) AS c FROM components WHERE wo_id=? AND step_seq=?",
                (wo_id, step_seq)).fetchone()["c"]
            # 每件产出（含报废件、返工件）都有序列组件
            if produced != int(s["total"]):
                ok = False
            if step_seq > 1:
                # 上道工序总产出 = 被本工序消耗 + 仍在制（未被消耗）
                consumed_rows = conn.execute(
                    "SELECT COUNT(*) AS links, COUNT(DISTINCT ci.input_component_id) AS consumed "
                    "FROM component_inputs ci JOIN components c ON c.component_id=ci.component_id "
                    "WHERE c.wo_id=? AND ci.step_seq=?", (wo_id, step_seq)).fetchone()
                prev_total = int(steps[step_seq - 1]["total"]) if step_seq - 1 in steps else 0
                unconsumed = conn.execute(
                    "SELECT COUNT(*) AS c FROM components WHERE wo_id=? AND step_seq=? "
                    "AND status<>'reworked' "
                    "AND component_id NOT IN (SELECT input_component_id FROM component_inputs)",
                    (wo_id, step_seq - 1)).fetchone()["c"]
                if consumed_rows["consumed"] + unconsumed != prev_total:
                    ok = False
                s = {**s, "consumed_inputs": consumed_rows["consumed"],
                     "input_links": consumed_rows["links"],
                     "previous_step_wip": unconsumed}
            step_balanced &= ok
            step_snapshots.append({"step_seq": step_seq, **s, "balanced": ok})
        return {"wo_id": wo_id, "status": wo["status"],
                "materials": lot_snapshots, "steps": step_snapshots,
                "balanced": material_balanced and step_balanced}

    # ------------------------------------------------------------------
    # 精准冻结 / 召回范围计算
    # ------------------------------------------------------------------

    def _expand_lot_descendants(self, conn, lot_ids: set[str]) -> set[str]:
        """沿拆包/重组谱系正向扩展受影响的库存批次。"""

        result, frontier = set(lot_ids), list(lot_ids)
        while frontier:
            current = frontier.pop()
            for row in conn.execute(
                    "SELECT child_lot_id FROM lot_lineage WHERE parent_lot_id=?", (current,)):
                if row["child_lot_id"] not in result:
                    result.add(row["child_lot_id"])
                    frontier.append(row["child_lot_id"])
        return result

    def _expand_components_descendants(self, conn, component_ids: set[str]) -> set[str]:
        """沿装配与返工关系正向扩展受影响组件。"""

        result, frontier = set(component_ids), list(component_ids)
        while frontier:
            current = frontier.pop()
            rows = conn.execute(
                "SELECT component_id FROM component_inputs WHERE input_component_id=? "
                "UNION SELECT parent_component_id FROM component_relations WHERE child_component_id=?",
                (current, current),
            ).fetchall()
            for row in rows:
                if row["component_id"] not in result:
                    result.add(row["component_id"])
                    frontier.append(row["component_id"])
        return result

    def _compute_impact(self, conn, *, source_kind: str, source_id: str) -> dict[str, Any]:
        """按真实用料关系计算冻结范围，不扩大到无关产品。"""

        seed_lots: set[str] = set()
        seed_components: set[str] = set()
        if source_kind == "lot":
            seed_lots.add(source_id)
        else:
            seed_components.add(source_id)
        lots = self._expand_lot_descendants(conn, seed_lots)
        # 批次 → 组件：优先按组件级用料归属精准定位（替代料场景），
        # 历史数据无归属时回退到产出级消耗关系。
        if lots:
            placeholders = ",".join("?" * len(lots))
            precise = {r["component_id"] for r in conn.execute(
                f"SELECT DISTINCT component_id FROM output_component_lots WHERE lot_id IN ({placeholders})",
                tuple(lots))}
            coarse = {r["component_id"] for r in conn.execute(
                f"SELECT DISTINCT c.component_id FROM material_consumption mc "
                f"JOIN process_outputs po ON po.output_id=mc.output_id "
                f"JOIN components c ON c.output_id=po.output_id WHERE mc.lot_id IN ({placeholders})",
                tuple(lots))}
            # 仅对没有任何组件级归属记录的产出使用粗粒度映射
            for comp_id in coarse:
                row = conn.execute(
                    "SELECT 1 FROM output_component_lots ocl JOIN components c ON c.component_id=ocl.component_id "
                    "WHERE c.component_id=?", (comp_id,)).fetchone()
                if not row:
                    precise.add(comp_id)
            seed_components |= precise
        components = self._expand_components_descendants(conn, seed_components)
        # 在制未消耗的批次只牵连其所在工单
        wos: set[str] = set()
        wip_by_wo_lot: dict[tuple[str, str], float] = {}
        for lot_id in lots:
            for row in conn.execute(
                    "SELECT wo_id, "
                    " COALESCE(SUM(CASE WHEN kind='issue' THEN -qty_delta END),0) "
                    "-COALESCE(SUM(CASE WHEN kind='return' THEN qty_delta END),0) "
                    "-COALESCE(SUM(CASE WHEN kind='wip_scrap' THEN -qty_delta END),0) AS input_qty "
                    "FROM material_transactions WHERE lot_id=? AND wo_id IS NOT NULL "
                    "GROUP BY wo_id HAVING input_qty > 0", (lot_id,)):
                consumed = conn.execute(
                    "SELECT COALESCE(SUM(mc.qty),0) AS s FROM material_consumption mc "
                    "JOIN process_outputs po ON po.output_id=mc.output_id "
                    "WHERE po.wo_id=? AND mc.lot_id=?", (row["wo_id"], lot_id),
                ).fetchone()["s"]
                wip = row["input_qty"] - consumed
                if wip > QTY_EPS:
                    wos.add(row["wo_id"])
                    wip_by_wo_lot[(row["wo_id"], lot_id)] = wip
        # 组件所在产出（审计用）
        if components:
            outputs = {r["output_id"] for r in conn.execute(
                "SELECT DISTINCT output_id FROM components WHERE component_id IN ({})".format(
                    ",".join("?" * len(components))), tuple(components))}
        else:
            outputs = set()
        packages, channel_batches, orders = set(), set(), set()
        for comp_id in components:
            row = conn.execute("SELECT package_id FROM package_items WHERE component_id=?",
                               (comp_id,)).fetchone()
            if row:
                packages.add(row["package_id"])
        for package_id in packages:
            row = conn.execute("SELECT channel_batch_id FROM packages WHERE package_id=?",
                               (package_id,)).fetchone()
            channel_batches.add(row["channel_batch_id"])
        # 只有当订单下全部渠道批次都受影响时才冻结订单本身，避免牵连无关批次
        for batch_id in channel_batches:
            row = conn.execute("SELECT order_id FROM channel_batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
            order_id = row["order_id"]
            all_batches = {r["batch_id"] for r in conn.execute(
                "SELECT batch_id FROM channel_batches WHERE order_id=?", (order_id,))}
            if all_batches <= channel_batches:
                orders.add(order_id)
        # 数量差异
        lot_quantities = {}
        for lot_id in sorted(lots):
            lot = conn.execute("SELECT remaining_qty FROM material_lots WHERE lot_id=?",
                               (lot_id,)).fetchone()
            consumed = conn.execute(
                "SELECT COALESCE(SUM(mc.qty),0) AS s FROM material_consumption mc WHERE mc.lot_id=?",
                (lot_id,)).fetchone()["s"]
            lot_quantities[lot_id] = {
                "remaining_qty": lot["remaining_qty"] if lot else 0.0,
                "consumed_qty": consumed,
                "wip_by_work_order": {wo: q for (wo, l), q in wip_by_wo_lot.items() if l == lot_id},
            }
        return {
            "lots": sorted(lots), "work_orders": sorted(wos),
            "outputs": sorted(outputs), "components": sorted(components),
            "packages": sorted(packages), "channel_batches": sorted(channel_batches),
            "orders": sorted(orders),
            "quantities": {
                "lots": lot_quantities,
                "component_count": len(components),
                "package_count": len(packages),
                "channel_batch_count": len(channel_batches),
                "order_count": len(orders),
            },
        }

    def _apply_freeze(self, conn, *, actor_id: str, source_kind: str, source_id: str,
                      reason: str, note: str) -> str:
        impact = self._compute_impact(conn, source_kind=source_kind, source_id=source_id)
        event_id = self._new_id()
        conn.execute(
            "INSERT INTO freeze_events(event_id,source_kind,source_id,reason,note,status,"
            "created_by,created_at,lifted_at,lifted_by) VALUES(?,?,?,?,?, 'active', ?,?,NULL,NULL)",
            (event_id, source_kind, source_id, reason, note, actor_id, self._now()),
        )
        for lot_id in impact["lots"]:
            conn.execute(
                "UPDATE material_lots SET pre_freeze_status=COALESCE(pre_freeze_status,status),"
                "status='frozen' WHERE lot_id=? AND status<>'closed'", (lot_id,))
        for comp_id in impact["components"]:
            conn.execute("UPDATE components SET frozen=1 WHERE component_id=?", (comp_id,))
        for wo_id in impact["work_orders"]:
            conn.execute(
                "UPDATE work_orders SET pre_freeze_status=COALESCE(pre_freeze_status,status),"
                "status='frozen' WHERE wo_id=? AND status NOT IN ('completed','closed')",
                (wo_id,))
        for package_id in impact["packages"]:
            conn.execute(
                "UPDATE packages SET pre_freeze_status=COALESCE(pre_freeze_status,status),"
                "status='frozen' WHERE package_id=?", (package_id,))
        for batch_id in impact["channel_batches"]:
            conn.execute(
                "UPDATE channel_batches SET pre_freeze_status=COALESCE(pre_freeze_status,status),"
                "status='frozen' WHERE batch_id=?", (batch_id,))
        for order_id in impact["orders"]:
            conn.execute(
                "UPDATE orders SET pre_freeze_status=COALESCE(pre_freeze_status,status),"
                "status='frozen' WHERE order_id=?", (order_id,))
        for entity_type, ids in (("lot", impact["lots"]), ("wo", impact["work_orders"]),
                                 ("output", impact["outputs"]),
                                 ("component", impact["components"]),
                                 ("package", impact["packages"]),
                                 ("channel_batch", impact["channel_batches"]),
                                 ("order", impact["orders"])):
            for entity_id in ids:
                conn.execute(
                    "INSERT INTO freeze_impacts(event_id,entity_type,entity_id) VALUES(?,?,?)",
                    (event_id, entity_type, entity_id),
                )
        self._audit(conn, actor_id, "pilot.freeze.applied", "freeze_event", event_id,
                    {"source_kind": source_kind, "source_id": source_id, "reason": reason,
                     "impact": {k: v for k, v in impact.items() if k != "quantities"},
                     "quantities": impact["quantities"]})
        return event_id

    def raise_freeze(self, *, request_id: str, actor_id: str, source_kind: str, source_id: str,
                     reason: str, note: str) -> PilotReceipt:
        """发现污染或标签错误时，沿真实用料关系只冻结受影响对象。"""

        if source_kind not in ("lot", "component"):
            raise ValidationError("source_kind 必须是 lot 或 component")
        if reason not in ("contamination", "mislabel", "inspection_fail"):
            raise ValidationError("reason 必须是 contamination/mislabel/inspection_fail")
        note = self._text(note, "note")
        payload = {"actor_id": actor_id, "source_kind": source_kind, "source_id": source_id,
                   "reason": reason, "note": note}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "raise_freeze", payload)
            if _replay is not None:
                return _replay
            source_id = self._id(source_id, "source_id")
            if source_kind == "lot":
                self._get(conn, "material_lots", "lot_id", source_id, "库存批次")
            else:
                self._get(conn, "components", "component_id", source_id, "组件")
            active = conn.execute(
                "SELECT event_id FROM freeze_events WHERE source_kind=? AND source_id=? AND status='active'",
                (source_kind, source_id),
            ).fetchall()
            if active:
                raise ConflictError("该来源已有生效中的冻结事件")

            def create():
                event_id = self._apply_freeze(conn, actor_id=actor_id, source_kind=source_kind,
                                              source_id=source_id, reason=reason, note=note)
                return "freeze_event", event_id, {"event_id": event_id}

            return self._idempotent(conn, request_id=request_id, action="raise_freeze",
                                    payload=payload, create=create)

    def lift_freeze(self, *, request_id: str, actor_id: str, event_id: str) -> PilotReceipt:
        """解除冻结；若实体仍被其他生效事件覆盖，则保持冻结。"""

        payload = {"actor_id": actor_id, "event_id": event_id}
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, ROLE_QC, ROLE_BRAND)
            _replay = self._replay_receipt(conn, request_id, "lift_freeze", payload)
            if _replay is not None:
                return _replay
            event_id = self._id(event_id, "event_id")
            event = self._get(conn, "freeze_events", "event_id", event_id, "冻结事件")
            if event["status"] != "active":
                raise ConflictError("冻结事件已解除")

            def create():
                conn.execute(
                    "UPDATE freeze_events SET status='lifted', lifted_at=?, lifted_by=? WHERE event_id=?",
                    (self._now(), actor_id, event_id),
                )
                impacted = conn.execute(
                    "SELECT entity_type, entity_id FROM freeze_impacts WHERE event_id=?",
                    (event_id,),
                ).fetchall()
                for row in impacted:
                    others = conn.execute(
                        "SELECT 1 FROM freeze_impacts fi JOIN freeze_events fe ON fe.event_id=fi.event_id "
                        "WHERE fi.entity_type=? AND fi.entity_id=? AND fe.status='active' "
                        "AND fi.event_id<>?",
                        (row["entity_type"], row["entity_id"], event_id),
                    ).fetchone()
                    if others:
                        continue
                    etype, eid = row["entity_type"], row["entity_id"]
                    if etype == "lot":
                        conn.execute(
                            "UPDATE material_lots SET status=COALESCE(pre_freeze_status,'available'),"
                            "pre_freeze_status=NULL WHERE lot_id=? AND status='frozen'", (eid,))
                    elif etype == "component":
                        conn.execute("UPDATE components SET frozen=0 WHERE component_id=?", (eid,))
                    elif etype == "wo":
                        conn.execute(
                            "UPDATE work_orders SET status=COALESCE(pre_freeze_status,'in_progress'),"
                            "pre_freeze_status=NULL WHERE wo_id=? AND status='frozen'", (eid,))
                    elif etype == "package":
                        conn.execute(
                            "UPDATE packages SET status=COALESCE(pre_freeze_status,'sealed'),"
                            "pre_freeze_status=NULL WHERE package_id=? AND status='frozen'", (eid,))
                    elif etype == "channel_batch":
                        conn.execute(
                            "UPDATE channel_batches SET status=COALESCE(pre_freeze_status,'open'),"
                            "pre_freeze_status=NULL WHERE batch_id=? AND status='frozen'", (eid,))
                    elif etype == "order":
                        conn.execute(
                            "UPDATE orders SET status=COALESCE(pre_freeze_status,'open'),"
                            "pre_freeze_status=NULL WHERE order_id=? AND status='frozen'", (eid,))
                self._audit(conn, actor_id, "pilot.freeze.lifted", "freeze_event", event_id, {})
                return "freeze_event", event_id, {"event_id": event_id, "status": "lifted"}

            return self._idempotent(conn, request_id=request_id, action="lift_freeze",
                                    payload=payload, create=create)

    def recall_scope(self, *, source_kind: str, source_id: str) -> FreezeImpact:
        """正向计算问题原料/组件的召回范围与数量差异（只读，不改变状态）。"""

        with self.database.transaction() as conn:
            impact = self._compute_impact(conn, source_kind=source_kind, source_id=source_id)
            return FreezeImpact(
                event_id="", source_kind=source_kind, source_id=source_id, reason="",
                lots=impact["lots"], work_orders=impact["work_orders"],
                components=impact["components"], packages=impact["packages"],
                channel_batches=impact["channel_batches"], orders=impact["orders"],
                quantities=impact["quantities"])

    # ------------------------------------------------------------------
    # 反向谱系追溯
    # ------------------------------------------------------------------

    def trace_component(self, component_id: str) -> dict[str, Any]:
        """从任一成品反查全部来源：组件谱系、原料批次、供应证明、包装去向。"""

        component_id = self._id(component_id, "component_id")
        conn = self.database.connection
        root = self._get(conn, "components", "component_id", component_id, "组件")
        nodes: list[LineageNode] = []
        seen = set()

        def walk(comp_id: str, depth: int, relation: str = "self") -> None:
            if comp_id in seen:
                return
            seen.add(comp_id)
            comp = conn.execute("SELECT * FROM components WHERE component_id=?",
                                (comp_id,)).fetchone()
            nodes.append(LineageNode(depth, "component", comp_id, relation,
                                     {"serial_no": comp["serial_no"], "status": comp["status"],
                                      "step_seq": comp["step_seq"], "frozen": bool(comp["frozen"]),
                                      "wo_id": comp["wo_id"]}).__dict__)
            for row in conn.execute(
                    "SELECT input_component_id FROM component_inputs WHERE component_id=? ORDER BY step_seq",
                    (comp_id,)):
                walk(row["input_component_id"], depth + 1, "assembled_from")
            for row in conn.execute(
                    "SELECT child_component_id FROM component_relations WHERE parent_component_id=? "
                    "AND relation='reworked_from'", (comp_id,)):
                walk(row["child_component_id"], depth + 1, "reworked_from")
            for row in conn.execute(
                    "SELECT ocl.lot_id, ocl.line_seq, ocl.qty, mb.batch_id, m.material_id, m.name, "
                    "mb.supplier_id, mb.cert_no, mb.origin_note, mb.delivery_note, ml.label "
                    "FROM output_component_lots ocl "
                    "JOIN material_lots ml ON ml.lot_id=ocl.lot_id "
                    "JOIN material_batches mb ON mb.batch_id=ml.batch_id "
                    "JOIN materials m ON m.material_id=mb.material_id "
                    "WHERE ocl.component_id=?", (comp_id,)):
                key = ("lot", row["lot_id"])
                if key in seen:
                    continue
                seen.add(key)
                nodes.append(LineageNode(
                    depth + 1, "lot", row["lot_id"], "consumed",
                    {"batch_id": row["batch_id"], "material_id": row["material_id"],
                     "material_name": row["name"], "label": row["label"],
                     "qty": row["qty"], "line_seq": row["line_seq"],
                     "supplier_id": row["supplier_id"], "cert_no": row["cert_no"],
                     "origin_note": row["origin_note"],
                     "delivery_note": row["delivery_note"]}).__dict__)
                for lr in conn.execute(
                        "SELECT parent_lot_id, reason, qty FROM lot_lineage WHERE child_lot_id=?",
                        (row["lot_id"],)):
                    pkey = ("lot-parent", lr["parent_lot_id"])
                    if pkey in seen:
                        continue
                    seen.add(pkey)
                    nodes.append(LineageNode(
                        depth + 2, "lot", lr["parent_lot_id"], lr["reason"],
                        {"qty": lr["qty"]}).__dict__)

        walk(component_id, 0)
        packages = []
        for row in conn.execute(
                "SELECT p.package_id,p.channel_batch_id,cb.order_id,o.channel "
                "FROM package_items pi JOIN packages p ON p.package_id=pi.package_id "
                "JOIN channel_batches cb ON cb.batch_id=p.channel_batch_id "
                "JOIN orders o ON o.order_id=cb.order_id WHERE pi.component_id=?",
                (component_id,)):
            packages.append({"package_id": row["package_id"],
                             "channel_batch_id": row["channel_batch_id"],
                             "order_id": row["order_id"], "channel": row["channel"]})
        wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?",
                          (root["wo_id"],)).fetchone()
        return {"component_id": component_id, "product_code": root["product_code"],
                "work_order": {"wo_id": wo["wo_id"], "recipe_id": wo["recipe_id"],
                               "recipe_version": wo["recipe_version"],
                               "planned_qty": wo["planned_qty"], "status": wo["status"]},
                "nodes": nodes, "packages": packages}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_lot(self, lot_id: str) -> dict[str, Any]:
        row = self._get(self.database.connection, "material_lots", "lot_id",
                        self._id(lot_id, "lot_id"), "库存批次")
        batch = self.database.connection.execute(
            "SELECT * FROM material_batches WHERE batch_id=?", (row["batch_id"],)).fetchone()
        return {"lot_id": row["lot_id"], "batch_id": row["batch_id"], "label": row["label"],
                "qty_received": row["qty_received"], "remaining_qty": row["remaining_qty"],
                "status": row["status"],
                "material_id": batch["material_id"], "supplier_id": batch["supplier_id"],
                "cert_no": batch["cert_no"], "origin_note": batch["origin_note"]}

    def get_work_order(self, wo_id: str) -> dict[str, Any]:
        row = self._get(self.database.connection, "work_orders", "wo_id",
                        self._id(wo_id, "wo_id"), "工单")
        return {k: row[k] for k in row.keys()}

    def get_component(self, component_id: str) -> dict[str, Any]:
        row = self._get(self.database.connection, "components", "component_id",
                        self._id(component_id, "component_id"), "组件")
        return {k: row[k] for k in row.keys()}

    def list_freeze_events(self, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM freeze_events"
        if active_only:
            query += " WHERE status='active'"
        query += " ORDER BY created_at"
        return [{k: row[k] for k in row.keys()}
                for row in self.database.connection.execute(query)]

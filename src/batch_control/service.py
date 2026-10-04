"""茶·道试产批次控制领域服务。

在基础服务的主体、幂等、审计与事务边界之上，实现试产批次的
原料批次与供应证明登记、检验项目、领退料、工序产出、组件序列、
包装组合、双人质量放行、替代料/拆包重组/追加抽检/返工谱系、
定向冻结与正反向追溯，并在每次事务后保持
投入 = 退料 + 合格 + 报废 + 返工 + 在制 的数量守恒。
"""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Callable

from creative_program_foundation.audit import append_event, canonical_json, digest
from creative_program_foundation.clock import Clock, SystemClock
from creative_program_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from creative_program_foundation.storage import Database

from .schema import ensure_schema


EPSILON = 1e-6
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 允许执行读操作的角色；写操作在各方法内单独限定。
READ_ROLES = ("admin", "operator", "reviewer", "auditor", "warehouse", "qc", "factory", "brand")

FREEZABLE_STATUS = {
    "material_lot": ("pending_inspection", "available", "quarantined"),
    "component": ("wip", "released", "rework", "quarantined"),
    "finished_unit": ("in_stock", "allocated", "shipped", "quarantined"),
    "sales_order": ("confirmed", "shipped"),
    "channel_batch": ("shipped",),
}

STATUS_TABLE = {
    "material_lot": ("material_lots", "lot_id"),
    "component": ("components", "component_id"),
    "finished_unit": ("finished_units", "unit_id"),
    "sales_order": ("sales_orders", "sales_order_id"),
    "channel_batch": ("channel_batches", "batch_id"),
}


class BatchControlService:
    """协调批次控制的权限、幂等、事务、守恒与谱系规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_schema(database)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _quantity(self, value: Any, field: str, allow_zero: bool = False) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是数字") from None
        if not math.isfinite(number):
            raise ValidationError(f"{field} 必须是有限数字")
        number = round(number, 6)
        if number < 0 or (number == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须{'为非负数' if allow_zero else '大于 0'}")
        return number

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """同一 request_id 重放时直接返回首次结果，绝不重复执行写入。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        response["replayed"] = False
        return response

    # ------------------------------------------------------------------
    # 行读取与业务校验
    # ------------------------------------------------------------------

    def _lot(self, connection, lot_id: str):
        row = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFoundError("原料批次不存在")
        return row

    def _order(self, connection, order_id: str):
        row = connection.execute("SELECT * FROM work_orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("工单不存在")
        return row

    def _step(self, connection, step_id: str):
        row = connection.execute("SELECT * FROM process_steps WHERE step_id=?", (step_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序不存在")
        return row

    def _component(self, connection, component_id: str):
        row = connection.execute("SELECT * FROM components WHERE component_id=?", (component_id,)).fetchone()
        if row is None:
            raise NotFoundError("组件不存在")
        return row

    def _output(self, connection, output_id: str):
        row = connection.execute("SELECT * FROM step_outputs WHERE output_id=?", (output_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序产出不存在")
        return row

    def _ledger(self, connection, step_id: str):
        row = connection.execute("SELECT * FROM step_ledgers WHERE step_id=?", (step_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序台账不存在")
        return row

    def _wip(self, ledger) -> float:
        """在制 = 投入 - 退料 - 合格 - 报废 - 返工，必须不为负。"""

        return round(ledger["received"] - ledger["returned"] - ledger["good"]
                     - ledger["scrap"] - ledger["rework"], 6)

    def _assert_conserved(self, connection, step_id: str) -> None:
        ledger = self._ledger(connection, step_id)
        if self._wip(ledger) < -EPSILON:
            raise ConflictError("工序数量不守恒：在制出现负值")

    def _active_recipe(self, connection, product_code: str):
        return connection.execute(
            "SELECT * FROM recipes WHERE product_code=? AND status='active'", (product_code,)
        ).fetchone()

    def _require_order_recipe_active(self, connection, order) -> None:
        row = connection.execute("SELECT * FROM recipes WHERE recipe_id=?", (order["recipe_id"],)).fetchone()
        if row is None or row["status"] != "active" or row["version"] != order["recipe_version"]:
            raise ConflictError("工单锁定的配方版本已不可用")

    def _inspections_of(self, connection, target_type: str, target_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM inspections WHERE target_type=? AND target_id=? ORDER BY created_at, inspection_id",
            (target_type, target_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _required_clear(self, connection, target_type: str, target_id: str) -> bool:
        """每个必检项目的最新结论均为合格，且至少有一项合格记录。

        按项目取最新结论，使返工后的复检可以覆盖此前的不合格记录，
        而历史检验全部保留在案。
        """

        rows = connection.execute(
            "SELECT item, result FROM inspections WHERE target_type=? AND target_id=? AND required=1 "
            "AND superseded=0 ORDER BY rowid",
            (target_type, target_id),
        ).fetchall()
        if not rows:
            return False
        latest: dict[str, str] = {}
        for row in rows:
            latest[row["item"]] = row["result"]
        return all(result == "pass" for result in latest.values())

    def _release_row(self, connection, target_type: str, target_id: str):
        return connection.execute(
            "SELECT * FROM releases WHERE target_type=? AND target_id=? AND status!='superseded' "
            "ORDER BY created_at DESC, release_id DESC LIMIT 1",
            (target_type, target_id),
        ).fetchone()

    def _released(self, connection, target_type: str, target_id: str) -> bool:
        row = self._release_row(connection, target_type, target_id)
        return bool(row and row["status"] == "released")

    def _supersede_releases(self, connection, target_type: str, target_id: str) -> None:
        connection.execute(
            "UPDATE releases SET status='superseded' WHERE target_type=? AND target_id=? AND status!='superseded'",
            (target_type, target_id),
        )

    def _add_edge(self, connection, *, parent_type: str, parent_id: str, child_type: str,
                  child_id: str, relation: str, quantity: float = 0.0, unit: str = "",
                  substituted_for: str | None, actor_id: str) -> None:
        connection.execute(
            "INSERT INTO genealogy_edges(edge_id,parent_type,parent_id,child_type,child_id,relation,"
            "quantity,unit,substituted_for,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, parent_type, parent_id, child_type, child_id, relation,
             quantity, unit, substituted_for, actor_id, self._now()),
        )

    # ------------------------------------------------------------------
    # 原料批次与供应证明（仓库）
    # ------------------------------------------------------------------

    def register_material_lot(self, *, request_id: str, actor_id: str, site_id: str, lot_id: str,
                              material_code: str, material_name: str, origin: str,
                              supplier_name: str, quantity: Any, unit: str,
                              certificates: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "lot_id": lot_id,
                   "material_code": material_code, "material_name": material_name, "origin": origin,
                   "supplier_name": supplier_name, "quantity": quantity, "unit": unit,
                   "certificates": certificates}
        site_id = self._identifier(site_id, "site_id")
        lot_id = self._identifier(lot_id, "lot_id")
        material_code = self._identifier(material_code, "material_code")
        material_name = self._text(material_name, "material_name")
        origin = self._text(origin, "origin")
        supplier_name = self._text(supplier_name, "supplier_name")
        unit = self._text(unit, "unit", 20)
        quantity = self._quantity(quantity, "quantity")
        if not isinstance(certificates, list) or not certificates:
            raise ValidationError("供应证明不能为空")
        normalized_certs = []
        for item in certificates:
            if not isinstance(item, dict):
                raise ValidationError("供应证明必须是对象")
            normalized_certs.append({
                "cert_type": self._text(item.get("cert_type", ""), "cert_type", 60),
                "cert_number": self._text(item.get("cert_number", ""), "cert_number", 80),
                "issuer": self._text(item.get("issuer", ""), "issuer", 120),
                "issued_on": self._text(item.get("issued_on", ""), "issued_on", 40),
                "file_hash": self._text(item.get("file_hash", ""), "file_hash", 128),
            })
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "warehouse", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO material_lots(lot_id,site_id,material_code,material_name,origin,supplier_name,"
                        "unit,quantity_received,quantity_on_hand,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'pending_inspection',?,?)",
                        (lot_id, site_id, material_code, material_name, origin, supplier_name,
                         unit, quantity, quantity, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("原料批次编号已经存在") from exc
                for cert in normalized_certs:
                    connection.execute(
                        "INSERT INTO supply_certificates(cert_id,lot_id,cert_type,cert_number,issuer,issued_on,"
                        "file_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, lot_id, cert["cert_type"], cert["cert_number"], cert["issuer"],
                         cert["issued_on"], cert["file_hash"], self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="batch.lot_registered",
                             resource_type="material_lot", resource_id=lot_id,
                             detail={"site_id": site_id, "material_code": material_code, "origin": origin,
                                     "supplier_name": supplier_name, "quantity": quantity, "unit": unit,
                                     "certificates": normalized_certs},
                             occurred_at=self._now())
                return "material_lot", lot_id, {"lot_id": lot_id, "status": "pending_inspection",
                                                "quantity_on_hand": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.register_material_lot", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 检验项目与追加抽检（质检）
    # ------------------------------------------------------------------

    def record_inspection(self, *, request_id: str, actor_id: str, target_type: str, target_id: str,
                          item: str, method: str, standard: str, result: str,
                          required: bool = True, kind: str = "initial",
                          measured_value: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "target_type": target_type, "target_id": target_id,
                   "item": item, "method": method, "standard": standard, "result": result,
                   "required": bool(required), "kind": kind, "measured_value": measured_value}
        if target_type not in ("material_lot", "component", "finished_batch"):
            raise ValidationError("检验对象类型无效")
        target_id = self._identifier(target_id, "target_id")
        item = self._text(item, "item", 120)
        method = self._text(method, "method", 120)
        standard = self._text(standard, "standard", 200)
        if result not in ("pending", "pass", "fail"):
            raise ValidationError("检验结论必须是 pending/pass/fail")
        if kind not in ("initial", "additional"):
            raise ValidationError("检验类别必须是 initial/additional")
        measured_value = str(measured_value or "")[:120]
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "qc", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                if target_type == "material_lot":
                    self._lot(connection, target_id)
                elif target_type == "component":
                    self._component(connection, target_id)
                else:
                    output = self._output(connection, target_id)
                    if output["output_kind"] != "finished_units":
                        raise ValidationError("finished_batch 检验对象必须是包装组合产出")
                inspection_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO inspections(inspection_id,target_type,target_id,item,method,standard,required,"
                    "kind,result,measured_value,inspector_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (inspection_id, target_type, target_id, item, method, standard, 1 if required else 0,
                     kind, result, measured_value, actor_id, self._now()),
                )
                new_status = None
                if target_type == "material_lot":
                    new_status = self._refresh_lot_status(connection, target_id)
                elif result == "fail" and required:
                    if target_type == "component":
                        component = self._component(connection, target_id)
                        if component["status"] not in ("consumed", "scrapped", "frozen"):
                            connection.execute("UPDATE components SET status='quarantined' WHERE component_id=?",
                                               (target_id,))
                            self._supersede_releases(connection, "component", target_id)
                            new_status = "quarantined"
                    else:
                        connection.execute(
                            "UPDATE finished_units SET status='quarantined' "
                            "WHERE output_id=? AND status IN ('in_stock','allocated')",
                            (target_id,),
                        )
                        self._supersede_releases(connection, "finished_batch", target_id)
                        new_status = "quarantined"
                append_event(connection, actor_id=actor_id, action="batch.inspection_recorded",
                             resource_type=target_type, resource_id=target_id,
                             detail={"inspection_id": inspection_id, "item": item, "kind": kind,
                                     "required": bool(required), "result": result,
                                     "measured_value": measured_value},
                             occurred_at=self._now())
                return "inspection", inspection_id, {"inspection_id": inspection_id,
                                                     "target_status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.record_inspection", payload=payload, create=create)

    def _refresh_lot_status(self, connection, lot_id: str) -> str:
        lot = self._lot(connection, lot_id)
        if lot["status"] == "frozen":
            return "frozen"
        rows = connection.execute(
            "SELECT item, result FROM inspections WHERE target_type='material_lot' AND target_id=? "
            "AND required=1 AND superseded=0 ORDER BY rowid",
            (lot_id,),
        ).fetchall()
        latest: dict[str, str] = {}
        for row in rows:
            latest[row["item"]] = row["result"]
        if any(result == "fail" for result in latest.values()):
            status = "quarantined"
        elif latest and all(result == "pass" for result in latest.values()):
            status = "available"
        else:
            status = "pending_inspection"
        if status != lot["status"]:
            connection.execute("UPDATE material_lots SET status=? WHERE lot_id=?", (status, lot_id))
        return status

    # ------------------------------------------------------------------
    # 双人质量放行（质检）
    # ------------------------------------------------------------------

    def approve_release(self, *, request_id: str, actor_id: str,
                        target_type: str, target_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "target_type": target_type, "target_id": target_id}
        if target_type not in ("component", "finished_batch"):
            raise ValidationError("放行对象类型无效")
        target_id = self._identifier(target_id, "target_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "qc", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._check_releasable(connection, target_type, target_id)
                existing = self._release_row(connection, target_type, target_id)
                if existing is None:
                    release_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO releases(release_id,target_type,target_id,status,first_actor,first_at,created_at) "
                        "VALUES(?,?,?,'pending_first',?,?,?)",
                        (release_id, target_type, target_id, actor_id, self._now(), self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="batch.release_first_signed",
                                 resource_type=target_type, resource_id=target_id,
                                 detail={"release_id": release_id}, occurred_at=self._now())
                    return "release", release_id, {"release_id": release_id, "status": "pending_first"}
                if existing["status"] == "released":
                    raise ConflictError("目标已完成双人放行")
                if existing["first_actor"] == actor_id:
                    raise ValidationError("双人放行需要不同的放行人员")
                connection.execute(
                    "UPDATE releases SET status='released', second_actor=?, second_at=? WHERE release_id=?",
                    (actor_id, self._now(), existing["release_id"]),
                )
                if target_type == "component":
                    connection.execute(
                        "UPDATE components SET status='released' WHERE component_id=? AND status='wip'",
                        (target_id,),
                    )
                append_event(connection, actor_id=actor_id, action="batch.release_completed",
                             resource_type=target_type, resource_id=target_id,
                             detail={"release_id": existing["release_id"],
                                     "first_actor": existing["first_actor"], "second_actor": actor_id},
                             occurred_at=self._now())
                return "release", existing["release_id"], {"release_id": existing["release_id"],
                                                           "status": "released"}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.approve_release", payload=payload, create=create)

    def _check_releasable(self, connection, target_type: str, target_id: str) -> None:
        if target_type == "component":
            component = self._component(connection, target_id)
            if component["status"] != "wip":
                raise ConflictError("组件当前状态不能放行")
            order = self._order(connection, component["order_id"])
            self._require_order_recipe_active(connection, order)
        else:
            output = self._output(connection, target_id)
            if output["output_kind"] != "finished_units":
                raise ValidationError("放行对象必须是包装组合产出")
            order = self._order(connection, output["order_id"])
            self._require_order_recipe_active(connection, order)
            blocked = connection.execute(
                "SELECT COUNT(*) AS count FROM finished_units WHERE output_id=? AND status IN ('quarantined','frozen')",
                (target_id,),
            ).fetchone()["count"]
            if blocked:
                raise ConflictError("包装组合内存在隔离或冻结成品")
        if not self._required_clear(connection, target_type, target_id):
            raise ConflictError("必检项目未全部合格，不能放行")

    # ------------------------------------------------------------------
    # 配方版本（管理员）
    # ------------------------------------------------------------------

    def create_recipe(self, *, request_id: str, actor_id: str, product_code: str,
                      version: int, lines: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "product_code": product_code,
                   "version": version, "lines": lines}
        product_code = self._identifier(product_code, "product_code")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValidationError("version 必须是正整数")
        if not isinstance(lines, list) or not lines:
            raise ValidationError("配方行不能为空")
        normalized = []
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                raise ValidationError("配方行必须是对象")
            substitutes = line.get("substitutes", [])
            if not isinstance(substitutes, list):
                raise ValidationError("替代料清单必须是数组")
            normalized.append({
                "line_no": index,
                "material_code": self._identifier(line.get("material_code", ""), "material_code"),
                "quantity": self._quantity(line.get("quantity"), "line quantity"),
                "unit": self._text(line.get("unit", ""), "line unit", 20),
                "substitutes": [self._identifier(item, "substitute") for item in substitutes],
            })
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                recipe_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO recipes(recipe_id,product_code,version,status,created_by,created_at) "
                        "VALUES(?,?,?,'draft',?,?)",
                        (recipe_id, product_code, version, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一产品的配方版本已经存在") from exc
                for line in normalized:
                    connection.execute(
                        "INSERT INTO recipe_lines(recipe_id,line_no,material_code,quantity,unit,substitutes_json) "
                        "VALUES(?,?,?,?,?,?)",
                        (recipe_id, line["line_no"], line["material_code"], line["quantity"],
                         line["unit"], canonical_json(line["substitutes"])),
                    )
                append_event(connection, actor_id=actor_id, action="batch.recipe_created",
                             resource_type="recipe", resource_id=recipe_id,
                             detail={"product_code": product_code, "version": version, "lines": normalized},
                             occurred_at=self._now())
                return "recipe", recipe_id, {"recipe_id": recipe_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.create_recipe", payload=payload, create=create)

    def activate_recipe(self, *, request_id: str, actor_id: str, recipe_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "recipe_id": recipe_id}
        recipe_id = self._identifier(recipe_id, "recipe_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                recipe = connection.execute("SELECT * FROM recipes WHERE recipe_id=?",
                                            (recipe_id,)).fetchone()
                if recipe is None:
                    raise NotFoundError("配方不存在")
                if recipe["status"] == "retired":
                    raise ConflictError("已停用的配方不能重新激活")
                connection.execute(
                    "UPDATE recipes SET status='retired' WHERE product_code=? AND status='active' AND recipe_id!=?",
                    (recipe["product_code"], recipe_id),
                )
                connection.execute("UPDATE recipes SET status='active' WHERE recipe_id=?", (recipe_id,))
                append_event(connection, actor_id=actor_id, action="batch.recipe_activated",
                             resource_type="recipe", resource_id=recipe_id,
                             detail={"product_code": recipe["product_code"], "version": recipe["version"]},
                             occurred_at=self._now())
                return "recipe", recipe_id, {"recipe_id": recipe_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.activate_recipe", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 工单与工序（工厂）
    # ------------------------------------------------------------------

    def create_work_order(self, *, request_id: str, actor_id: str, order_id: str, site_id: str,
                          product_code: str, planned_quantity: Any, unit: str,
                          steps: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id, "site_id": site_id,
                   "product_code": product_code, "planned_quantity": planned_quantity,
                   "unit": unit, "steps": steps}
        order_id = self._identifier(order_id, "order_id")
        site_id = self._identifier(site_id, "site_id")
        product_code = self._identifier(product_code, "product_code")
        unit = self._text(unit, "unit", 20)
        planned_quantity = self._quantity(planned_quantity, "planned_quantity")
        if not isinstance(steps, list) or not steps:
            raise ValidationError("工序列表不能为空")
        normalized_steps = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise ValidationError("工序必须是对象")
            normalized_steps.append({
                "seq": index,
                "name": self._text(step.get("name", ""), "step name", 80),
                "unit": self._text(step.get("unit", ""), "step unit", 20),
                "is_packaging": bool(step.get("is_packaging", False)),
            })
        if not any(step["is_packaging"] for step in normalized_steps):
            normalized_steps[-1]["is_packaging"] = True
        for step in normalized_steps[:-1]:
            if step["is_packaging"]:
                raise ValidationError("包装组合工序只能是最后一道工序")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                recipe = self._active_recipe(connection, product_code)
                if recipe is None:
                    raise ConflictError("产品没有处于启用状态的配方版本")
                try:
                    connection.execute(
                        "INSERT INTO work_orders(order_id,site_id,product_code,recipe_id,recipe_version,"
                        "planned_quantity,unit,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
                        (order_id, site_id, product_code, recipe["recipe_id"], recipe["version"],
                         planned_quantity, unit, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("工单编号已经存在") from exc
                step_ids = []
                for step in normalized_steps:
                    step_id = f"{order_id}-S{step['seq']}"
                    connection.execute(
                        "INSERT INTO process_steps(step_id,order_id,seq,name,unit,is_packaging) VALUES(?,?,?,?,?,?)",
                        (step_id, order_id, step["seq"], step["name"], step["unit"],
                         1 if step["is_packaging"] else 0),
                    )
                    connection.execute(
                        "INSERT INTO step_ledgers(step_id,order_id,unit) VALUES(?,?,?)",
                        (step_id, order_id, step["unit"]),
                    )
                    step_ids.append(step_id)
                append_event(connection, actor_id=actor_id, action="batch.work_order_created",
                             resource_type="work_order", resource_id=order_id,
                             detail={"site_id": site_id, "product_code": product_code,
                                     "recipe_id": recipe["recipe_id"], "recipe_version": recipe["version"],
                                     "planned_quantity": planned_quantity, "unit": unit,
                                     "steps": normalized_steps},
                             occurred_at=self._now())
                return "work_order", order_id, {"order_id": order_id, "recipe_id": recipe["recipe_id"],
                                                "recipe_version": recipe["version"], "step_ids": step_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.create_work_order", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 领料与退料（仓库）
    # ------------------------------------------------------------------

    def issue_material(self, *, request_id: str, actor_id: str, order_id: str, step_id: str,
                       lot_id: str, quantity: Any, substituted_for: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id, "step_id": step_id, "lot_id": lot_id,
                   "quantity": quantity, "substituted_for": substituted_for}
        order_id = self._identifier(order_id, "order_id")
        step_id = self._identifier(step_id, "step_id")
        lot_id = self._identifier(lot_id, "lot_id")
        quantity = self._quantity(quantity, "quantity")
        if substituted_for is not None:
            substituted_for = self._identifier(substituted_for, "substituted_for")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "warehouse", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                order = self._order(connection, order_id)
                if order["status"] not in ("open", "in_progress"):
                    raise ConflictError("工单已关闭，不能领料")
                self._require_order_recipe_active(connection, order)
                step = self._step(connection, step_id)
                if step["order_id"] != order_id:
                    raise ValidationError("工序不属于该工单")
                lot = self._lot(connection, lot_id)
                if lot["unit"] != step["unit"]:
                    raise ValidationError("批次单位与工序单位不一致")
                kind, base_code = self._check_recipe_material(connection, order, lot["material_code"],
                                                              substituted_for)
                # 条件更新保证并发领料不会出现负库存。
                cursor = connection.execute(
                    "UPDATE material_lots SET quantity_on_hand = quantity_on_hand - ? "
                    "WHERE lot_id=? AND status='available' AND quantity_on_hand >= ?",
                    (quantity, lot_id, quantity),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("批次库存不足或未处于可用状态")
                issue_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO material_issues(issue_id,order_id,step_id,lot_id,material_code,quantity,unit,"
                    "kind,substituted_for,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (issue_id, order_id, step_id, lot_id, lot["material_code"], quantity, lot["unit"],
                     kind, substituted_for if kind == "substitute" else None, actor_id, self._now()),
                )
                connection.execute("UPDATE step_ledgers SET received = received + ? WHERE step_id=?",
                                   (quantity, step_id))
                connection.execute("UPDATE work_orders SET status='in_progress' WHERE order_id=? AND status='open'",
                                   (order_id,))
                self._add_edge(connection, parent_type="material_lot", parent_id=lot_id,
                               child_type="step", child_id=step_id,
                               relation="substitute" if kind == "substitute" else "consume",
                               quantity=quantity, unit=lot["unit"],
                               substituted_for=substituted_for if kind == "substitute" else None,
                               actor_id=actor_id)
                self._assert_conserved(connection, step_id)
                on_hand = self._lot(connection, lot_id)["quantity_on_hand"]
                append_event(connection, actor_id=actor_id, action="batch.material_issued",
                             resource_type="material_lot", resource_id=lot_id,
                             detail={"issue_id": issue_id, "order_id": order_id, "step_id": step_id,
                                     "quantity": quantity, "unit": lot["unit"], "kind": kind,
                                     "substituted_for": substituted_for if kind == "substitute" else None},
                             occurred_at=self._now())
                return "material_issue", issue_id, {"issue_id": issue_id, "kind": kind,
                                                    "lot_on_hand": on_hand}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.issue_material", payload=payload, create=create)

    def _check_recipe_material(self, connection, order, material_code: str,
                               substituted_for: str | None) -> tuple[str, str]:
        lines = connection.execute(
            "SELECT * FROM recipe_lines WHERE recipe_id=? ORDER BY line_no", (order["recipe_id"],)
        ).fetchall()
        if substituted_for is None:
            if any(line["material_code"] == material_code for line in lines):
                return "normal", material_code
            raise ValidationError("物料不在工单锁定的配方中")
        for line in lines:
            if line["material_code"] == substituted_for:
                substitutes = json.loads(line["substitutes_json"])
                if material_code in substitutes:
                    return "substitute", substituted_for
                raise ValidationError("该物料不是配方允许的替代料")
        raise ValidationError("被替代的物料不在配方中")

    def return_material(self, *, request_id: str, actor_id: str, order_id: str, step_id: str,
                        lot_id: str, quantity: Any) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id, "step_id": step_id,
                   "lot_id": lot_id, "quantity": quantity}
        order_id = self._identifier(order_id, "order_id")
        step_id = self._identifier(step_id, "step_id")
        lot_id = self._identifier(lot_id, "lot_id")
        quantity = self._quantity(quantity, "quantity")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "warehouse", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                order = self._order(connection, order_id)
                if order["status"] not in ("open", "in_progress"):
                    raise ConflictError("工单已关闭，不能退料")
                step = self._step(connection, step_id)
                if step["order_id"] != order_id:
                    raise ValidationError("工序不属于该工单")
                lot = self._lot(connection, lot_id)
                ledger = self._ledger(connection, step_id)
                if quantity > self._wip(ledger) + EPSILON:
                    raise ConflictError("可退数量不能超过工序在制数量")
                connection.execute(
                    "UPDATE material_lots SET quantity_on_hand = quantity_on_hand + ? WHERE lot_id=?",
                    (quantity, lot_id),
                )
                connection.execute("UPDATE step_ledgers SET returned = returned + ? WHERE step_id=?",
                                   (quantity, step_id))
                return_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO material_returns(return_id,order_id,step_id,lot_id,quantity,unit,created_by,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (return_id, order_id, step_id, lot_id, quantity, lot["unit"], actor_id, self._now()),
                )
                self._assert_conserved(connection, step_id)
                append_event(connection, actor_id=actor_id, action="batch.material_returned",
                             resource_type="material_lot", resource_id=lot_id,
                             detail={"return_id": return_id, "order_id": order_id, "step_id": step_id,
                                     "quantity": quantity, "unit": lot["unit"]},
                             occurred_at=self._now())
                return "material_return", return_id, {"return_id": return_id,
                                                      "lot_on_hand": self._lot(connection, lot_id)["quantity_on_hand"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.return_material", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 工序产出与组件序列（工厂）
    # ------------------------------------------------------------------

    def report_output(self, *, request_id: str, actor_id: str, order_id: str, step_id: str,
                      good_quantity: Any, scrap_quantity: Any = 0, rework_quantity: Any = 0,
                      outputs: list[dict[str, Any]] | None = None,
                      inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id, "step_id": step_id,
                   "good_quantity": good_quantity, "scrap_quantity": scrap_quantity,
                   "rework_quantity": rework_quantity, "outputs": outputs, "inputs": inputs}
        order_id = self._identifier(order_id, "order_id")
        step_id = self._identifier(step_id, "step_id")
        good_quantity = self._quantity(good_quantity, "good_quantity", allow_zero=True)
        scrap_quantity = self._quantity(scrap_quantity, "scrap_quantity", allow_zero=True)
        rework_quantity = self._quantity(rework_quantity, "rework_quantity", allow_zero=True)
        outputs = outputs or []
        inputs = inputs or []
        if not isinstance(outputs, list) or not isinstance(inputs, list):
            raise ValidationError("outputs 与 inputs 必须是数组")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                order = self._order(connection, order_id)
                if order["status"] not in ("open", "in_progress"):
                    raise ConflictError("工单已关闭，不能报工")
                step = self._step(connection, step_id)
                if step["order_id"] != order_id:
                    raise ValidationError("工序不属于该工单")
                self._require_order_recipe_active(connection, order)
                is_packaging = bool(step["is_packaging"])
                # 产出序列与合格数量必须一致。
                normalized_outputs = []
                if is_packaging:
                    if scrap_quantity or rework_quantity:
                        pass  # 包装工序同样允许报废与返工数量
                    if abs(len(outputs) - good_quantity) > EPSILON:
                        raise ValidationError("成品序列数量必须与合格数量一致")
                    for item in outputs:
                        normalized_outputs.append({
                            "serial_no": self._identifier(item.get("serial_no", ""), "serial_no"),
                        })
                else:
                    total = 0.0
                    for item in outputs:
                        serial_no = self._identifier(item.get("serial_no", ""), "serial_no")
                        code = self._identifier(item.get("component_code", ""), "component_code")
                        qty = self._quantity(item.get("quantity"), "component quantity")
                        total += qty
                        normalized_outputs.append({"serial_no": serial_no, "component_code": code,
                                                   "quantity": qty})
                    if abs(total - good_quantity) > EPSILON:
                        raise ValidationError("组件数量合计必须与合格数量一致")
                    if good_quantity > EPSILON and not normalized_outputs:
                        raise ValidationError("合格产出必须登记组件序列")
                # 投入组件必须满足配方版本、检验与双人放行条件。
                normalized_inputs = []
                for item in inputs:
                    component = self._component(connection, str(item.get("component_id", "")))
                    qty = self._quantity(item.get("quantity"), "input quantity")
                    self._check_component_usable(connection, component, order, step)
                    if not is_packaging and component["unit"] != step["unit"]:
                        raise ValidationError("组件单位与工序单位不一致")
                    normalized_inputs.append({"component": component, "quantity": qty})
                # 守恒：本次合格+报废+返工不能超过当前在制（含本次投入的组件）。
                ledger = self._ledger(connection, step_id)
                wip = self._wip(ledger)
                if not is_packaging:
                    wip = round(wip + sum(item["quantity"] for item in normalized_inputs), 6)
                if good_quantity + scrap_quantity + rework_quantity > wip + EPSILON:
                    raise ConflictError("工序在制数量不足，不能报工")
                output_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO step_outputs(output_id,order_id,step_id,output_kind,good_quantity,"
                    "scrap_quantity,unit,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (output_id, order_id, step_id,
                     "finished_units" if is_packaging else "components",
                     good_quantity, scrap_quantity, step["unit"], actor_id, self._now()),
                )
                created_serials = []
                if is_packaging:
                    for item in normalized_outputs:
                        unit_id = uuid.uuid4().hex
                        try:
                            connection.execute(
                                "INSERT INTO finished_units(unit_id,serial_no,order_id,output_id,product_code,"
                                "status,created_at) VALUES(?,?,?,?,?,'in_stock',?)",
                                (unit_id, item["serial_no"], order_id, output_id,
                                 order["product_code"], self._now()),
                            )
                        except Exception as exc:
                            raise ConflictError(f"成品序列号 {item['serial_no']} 已经存在") from exc
                        created_serials.append(item["serial_no"])
                else:
                    for item in normalized_outputs:
                        component_id = uuid.uuid4().hex
                        try:
                            connection.execute(
                                "INSERT INTO components(component_id,serial_no,order_id,step_id,output_id,"
                                "component_code,quantity,remaining_qty,unit,status,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'wip',?)",
                                (component_id, item["serial_no"], order_id, step_id, output_id,
                                 item["component_code"], item["quantity"], item["quantity"],
                                 step["unit"], self._now()),
                            )
                        except Exception as exc:
                            raise ConflictError(f"组件序列号 {item['serial_no']} 已经存在") from exc
                        created_serials.append(item["serial_no"])
                for item in normalized_inputs:
                    component = item["component"]
                    qty = item["quantity"]
                    cursor = connection.execute(
                        "UPDATE components SET remaining_qty = remaining_qty - ? "
                        "WHERE component_id=? AND status='released' AND remaining_qty >= ?",
                        (qty, component["component_id"], qty),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("组件可用数量不足")
                    connection.execute(
                        "UPDATE components SET status='consumed' WHERE component_id=? AND remaining_qty <= ?",
                        (component["component_id"], EPSILON),
                    )
                    if is_packaging:
                        relation = "repack" if self._has_split_history(connection, component["component_id"]) \
                            else "combine"
                    else:
                        relation = "consume"
                        connection.execute(
                            "UPDATE step_ledgers SET received = received + ? WHERE step_id=?",
                            (qty, step_id),
                        )
                    self._add_edge(connection, parent_type="component",
                                   parent_id=component["component_id"],
                                   child_type="step_output", child_id=output_id, relation=relation,
                                   quantity=qty, unit=component["unit"],
                                   substituted_for=None, actor_id=actor_id)
                connection.execute(
                    "UPDATE step_ledgers SET good = good + ?, scrap = scrap + ?, rework = rework + ? "
                    "WHERE step_id=?",
                    (good_quantity, scrap_quantity, rework_quantity, step_id),
                )
                connection.execute("UPDATE work_orders SET status='in_progress' WHERE order_id=? AND status='open'",
                                   (order_id,))
                self._assert_conserved(connection, step_id)
                append_event(connection, actor_id=actor_id, action="batch.output_reported",
                             resource_type="step_output", resource_id=output_id,
                             detail={"order_id": order_id, "step_id": step_id, "good": good_quantity,
                                     "scrap": scrap_quantity, "rework": rework_quantity,
                                     "serials": created_serials,
                                     "inputs": [{"component_id": item["component"]["component_id"],
                                                 "quantity": item["quantity"]}
                                                for item in normalized_inputs]},
                             occurred_at=self._now())
                return "step_output", output_id, {"output_id": output_id, "serials": created_serials}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.report_output", payload=payload, create=create)

    def _has_split_history(self, connection, component_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM genealogy_edges WHERE child_type='component' AND child_id=? AND relation='split' LIMIT 1",
            (component_id,),
        ).fetchone()
        return row is not None

    def _check_component_usable(self, connection, component, order, step) -> None:
        """只有满足配方版本、检验与双人放行条件的组件才能进入下一工序。"""

        if component["order_id"] != order["order_id"]:
            raise ValidationError("组件不属于该工单")
        source_step = self._step(connection, component["step_id"])
        if source_step["seq"] >= step["seq"]:
            raise ValidationError("组件只能流向后续工序")
        if component["status"] != "released":
            raise ConflictError("组件未完成双人放行或已被冻结/隔离")
        if not self._released(connection, "component", component["component_id"]):
            raise ConflictError("组件缺少有效的双人放行记录")
        if not self._required_clear(connection, "component", component["component_id"]):
            raise ConflictError("组件必检项目未全部合格")

    # ------------------------------------------------------------------
    # 返工（工厂）
    # ------------------------------------------------------------------

    def resolve_rework(self, *, request_id: str, actor_id: str, order_id: str, step_id: str,
                       quantity: Any, outcome: str,
                       outputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """处理报工时登记的匿名返工数量：补回合格（登记新序列）或转为报废。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "step_id": step_id,
                   "quantity": quantity, "outcome": outcome, "outputs": outputs}
        order_id = self._identifier(order_id, "order_id")
        step_id = self._identifier(step_id, "step_id")
        quantity = self._quantity(quantity, "quantity")
        if outcome not in ("good", "scrap"):
            raise ValidationError("返工处理结果必须是 good/scrap")
        outputs = outputs or []
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                order = self._order(connection, order_id)
                step = self._step(connection, step_id)
                if step["order_id"] != order_id or bool(step["is_packaging"]):
                    raise ValidationError("工序无效")
                ledger = self._ledger(connection, step_id)
                if quantity > ledger["rework"] + EPSILON:
                    raise ConflictError("返工数量超过账面返工余额")
                created_serials: list[str] = []
                if outcome == "good":
                    total = 0.0
                    normalized = []
                    for item in outputs:
                        serial_no = self._identifier(item.get("serial_no", ""), "serial_no")
                        code = self._identifier(item.get("component_code", ""), "component_code")
                        qty = self._quantity(item.get("quantity"), "component quantity")
                        total += qty
                        normalized.append({"serial_no": serial_no, "component_code": code, "quantity": qty})
                    if abs(total - quantity) > EPSILON or not normalized:
                        raise ValidationError("返工合格品必须登记与数量一致的组件序列")
                    output_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO step_outputs(output_id,order_id,step_id,output_kind,good_quantity,"
                        "scrap_quantity,unit,created_by,created_at) VALUES(?,?,?,'components',?,0,?,?,?)",
                        (output_id, order_id, step_id, quantity, step["unit"], actor_id, self._now()),
                    )
                    for item in normalized:
                        component_id = uuid.uuid4().hex
                        try:
                            connection.execute(
                                "INSERT INTO components(component_id,serial_no,order_id,step_id,output_id,"
                                "component_code,quantity,remaining_qty,unit,status,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'wip',?)",
                                (component_id, item["serial_no"], order_id, step_id, output_id,
                                 item["component_code"], item["quantity"], item["quantity"],
                                 step["unit"], self._now()),
                            )
                        except Exception as exc:
                            raise ConflictError(f"组件序列号 {item['serial_no']} 已经存在") from exc
                        created_serials.append(item["serial_no"])
                    connection.execute(
                        "UPDATE step_ledgers SET good = good + ?, rework = rework - ? WHERE step_id=?",
                        (quantity, quantity, step_id),
                    )
                else:
                    connection.execute(
                        "UPDATE step_ledgers SET scrap = scrap + ?, rework = rework - ? WHERE step_id=?",
                        (quantity, quantity, step_id),
                    )
                self._assert_conserved(connection, step_id)
                append_event(connection, actor_id=actor_id, action="batch.rework_resolved",
                             resource_type="step", resource_id=step_id,
                             detail={"order_id": order_id, "quantity": quantity, "outcome": outcome,
                                     "serials": created_serials},
                             occurred_at=self._now())
                return "step", step_id, {"step_id": step_id, "outcome": outcome,
                                         "serials": created_serials}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.resolve_rework", payload=payload, create=create)

    def rework_component(self, *, request_id: str, actor_id: str, component_id: str,
                         note: str = "") -> dict[str, Any]:
        """把隔离/待检组件退回返工，原始谱系全部保留。"""

        payload = {"actor_id": actor_id, "component_id": component_id, "note": note}
        component_id = self._identifier(component_id, "component_id")
        note = str(note or "")[:200]
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                component = self._component(connection, component_id)
                if component["status"] not in ("wip", "quarantined"):
                    raise ConflictError("只有待检或隔离状态的组件可以返工")
                if component["remaining_qty"] < component["quantity"] - EPSILON:
                    raise ConflictError("组件已被部分消耗，不能整体返工")
                connection.execute("UPDATE components SET status='rework' WHERE component_id=?",
                                   (component_id,))
                connection.execute(
                    "UPDATE step_ledgers SET good = good - ?, rework = rework + ? WHERE step_id=?",
                    (component["quantity"], component["quantity"], component["step_id"]),
                )
                self._supersede_releases(connection, "component", component_id)
                self._add_edge(connection, parent_type="component", parent_id=component_id,
                               child_type="step", child_id=component["step_id"], relation="rework",
                               quantity=component["quantity"], unit=component["unit"],
                               substituted_for=None, actor_id=actor_id)
                self._assert_conserved(connection, component["step_id"])
                append_event(connection, actor_id=actor_id, action="batch.component_rework_started",
                             resource_type="component", resource_id=component_id,
                             detail={"note": note, "quantity": component["quantity"]},
                             occurred_at=self._now())
                return "component", component_id, {"component_id": component_id, "status": "rework"}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.rework_component", payload=payload, create=create)

    def resolve_component_rework(self, *, request_id: str, actor_id: str, component_id: str,
                                 outcome: str) -> dict[str, Any]:
        """返工组件归位：修复后回到待检（需重新检验与双人放行），或转为报废。"""

        payload = {"actor_id": actor_id, "component_id": component_id, "outcome": outcome}
        component_id = self._identifier(component_id, "component_id")
        if outcome not in ("good", "scrap"):
            raise ValidationError("返工处理结果必须是 good/scrap")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                component = self._component(connection, component_id)
                if component["status"] != "rework":
                    raise ConflictError("组件不在返工状态")
                if outcome == "good":
                    connection.execute("UPDATE components SET status='wip' WHERE component_id=?",
                                       (component_id,))
                    # 返工前的必检结论随物理重生失效，需重新检验与双人放行。
                    connection.execute(
                        "UPDATE inspections SET superseded=1 WHERE target_type='component' "
                        "AND target_id=? AND required=1",
                        (component_id,),
                    )
                    connection.execute(
                        "UPDATE step_ledgers SET good = good + ?, rework = rework - ? WHERE step_id=?",
                        (component["quantity"], component["quantity"], component["step_id"]),
                    )
                    new_status = "wip"
                else:
                    connection.execute(
                        "UPDATE components SET status='scrapped', remaining_qty=0 WHERE component_id=?",
                        (component_id,),
                    )
                    connection.execute(
                        "UPDATE step_ledgers SET scrap = scrap + ?, rework = rework - ? WHERE step_id=?",
                        (component["quantity"], component["quantity"], component["step_id"]),
                    )
                    new_status = "scrapped"
                self._assert_conserved(connection, component["step_id"])
                append_event(connection, actor_id=actor_id, action="batch.component_rework_resolved",
                             resource_type="component", resource_id=component_id,
                             detail={"outcome": outcome}, occurred_at=self._now())
                return "component", component_id, {"component_id": component_id, "status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.resolve_component_rework", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 拆包重组（仓库/工厂）
    # ------------------------------------------------------------------

    def unpack_output(self, *, request_id: str, actor_id: str, output_id: str) -> dict[str, Any]:
        """拆开整个包装组合：成品注销、组件回到已放行库存，谱系边全部保留。"""

        payload = {"actor_id": actor_id, "output_id": output_id}
        output_id = self._identifier(output_id, "output_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "warehouse", "factory", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                output = self._output(connection, output_id)
                if output["output_kind"] != "finished_units":
                    raise ValidationError("只有包装组合产出可以拆包")
                blocked = connection.execute(
                    "SELECT COUNT(*) AS count FROM finished_units WHERE output_id=? AND status!='in_stock'",
                    (output_id,),
                ).fetchone()["count"]
                if blocked:
                    raise ConflictError("组合内存在已售、已拆或冻结成品，不能拆包")
                connection.execute("UPDATE finished_units SET status='unpacked' WHERE output_id=?",
                                   (output_id,))
                restored = []
                edges = connection.execute(
                    "SELECT * FROM genealogy_edges WHERE child_type='step_output' AND child_id=? "
                    "AND parent_type='component' AND relation IN ('combine','repack')",
                    (output_id,),
                ).fetchall()
                for edge in edges:
                    connection.execute(
                        "UPDATE components SET remaining_qty = remaining_qty + ? WHERE component_id=?",
                        (edge["quantity"], edge["parent_id"]),
                    )
                    connection.execute(
                        "UPDATE components SET status='released' WHERE component_id=? AND status='consumed'",
                        (edge["parent_id"],),
                    )
                    self._add_edge(connection, parent_type="step_output", parent_id=output_id,
                                   child_type="component", child_id=edge["parent_id"], relation="split",
                                   quantity=edge["quantity"], unit=edge["unit"],
                                   substituted_for=None, actor_id=actor_id)
                    restored.append({"component_id": edge["parent_id"], "quantity": edge["quantity"]})
                connection.execute(
                    "UPDATE step_ledgers SET good = good - ? WHERE step_id=?",
                    (output["good_quantity"], output["step_id"]),
                )
                self._supersede_releases(connection, "finished_batch", output_id)
                self._assert_conserved(connection, output["step_id"])
                append_event(connection, actor_id=actor_id, action="batch.output_unpacked",
                             resource_type="step_output", resource_id=output_id,
                             detail={"restored": restored}, occurred_at=self._now())
                return "step_output", output_id, {"output_id": output_id, "restored": restored}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.unpack_output", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 销售订单与渠道批次（品牌方）
    # ------------------------------------------------------------------

    def create_sales_order(self, *, request_id: str, actor_id: str, sales_order_id: str,
                           channel: str, serials: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "sales_order_id": sales_order_id,
                   "channel": channel, "serials": serials}
        sales_order_id = self._identifier(sales_order_id, "sales_order_id")
        channel = self._text(channel, "channel", 120)
        if not isinstance(serials, list) or not serials:
            raise ValidationError("成品序列不能为空")
        normalized_serials = [self._identifier(item, "serial_no") for item in serials]
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "brand", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                unit_ids = []
                for serial in normalized_serials:
                    unit = connection.execute(
                        "SELECT * FROM finished_units WHERE serial_no=?", (serial,)
                    ).fetchone()
                    if unit is None:
                        raise NotFoundError(f"成品序列 {serial} 不存在")
                    if unit["status"] != "in_stock":
                        raise ConflictError(f"成品 {serial} 不在可售库存中")
                    if not self._released(connection, "finished_batch", unit["output_id"]):
                        raise ConflictError(f"成品 {serial} 所属包装组合未完成双人放行")
                    if not self._required_clear(connection, "finished_batch", unit["output_id"]):
                        raise ConflictError(f"成品 {serial} 所属包装组合检验未全部合格")
                    unit_ids.append(unit["unit_id"])
                try:
                    connection.execute(
                        "INSERT INTO sales_orders(sales_order_id,channel,status,created_by,created_at) "
                        "VALUES(?,?,'confirmed',?,?)",
                        (sales_order_id, channel, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("销售订单编号已经存在") from exc
                for unit_id in unit_ids:
                    connection.execute(
                        "INSERT INTO sales_order_units(sales_order_id,unit_id) VALUES(?,?)",
                        (sales_order_id, unit_id),
                    )
                    connection.execute(
                        "UPDATE finished_units SET status='allocated' WHERE unit_id=?", (unit_id,)
                    )
                append_event(connection, actor_id=actor_id, action="batch.sales_order_created",
                             resource_type="sales_order", resource_id=sales_order_id,
                             detail={"channel": channel, "serials": normalized_serials},
                             occurred_at=self._now())
                return "sales_order", sales_order_id, {"sales_order_id": sales_order_id,
                                                       "units": len(unit_ids)}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.create_sales_order", payload=payload, create=create)

    def ship_order(self, *, request_id: str, actor_id: str, sales_order_id: str,
                   batch_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "sales_order_id": sales_order_id, "batch_id": batch_id}
        sales_order_id = self._identifier(sales_order_id, "sales_order_id")
        batch_id = self._identifier(batch_id, "batch_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "brand", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                order = connection.execute(
                    "SELECT * FROM sales_orders WHERE sales_order_id=?", (sales_order_id,)
                ).fetchone()
                if order is None:
                    raise NotFoundError("销售订单不存在")
                if order["status"] != "confirmed":
                    raise ConflictError("订单当前状态不能发货")
                try:
                    connection.execute(
                        "INSERT INTO channel_batches(batch_id,sales_order_id,channel,status,created_by,created_at) "
                        "VALUES(?,?,?,'shipped',?,?)",
                        (batch_id, sales_order_id, order["channel"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("渠道批次编号已经存在") from exc
                units = connection.execute(
                    "SELECT unit_id FROM sales_order_units WHERE sales_order_id=?", (sales_order_id,)
                ).fetchall()
                for row in units:
                    connection.execute(
                        "INSERT INTO channel_batch_units(batch_id,unit_id) VALUES(?,?)",
                        (batch_id, row["unit_id"]),
                    )
                    connection.execute(
                        "UPDATE finished_units SET status='shipped' WHERE unit_id=? AND status='allocated'",
                        (row["unit_id"],),
                    )
                connection.execute(
                    "UPDATE sales_orders SET status='shipped' WHERE sales_order_id=?", (sales_order_id,)
                )
                append_event(connection, actor_id=actor_id, action="batch.channel_batch_shipped",
                             resource_type="channel_batch", resource_id=batch_id,
                             detail={"sales_order_id": sales_order_id, "channel": order["channel"],
                                     "units": len(units)},
                             occurred_at=self._now())
                return "channel_batch", batch_id, {"batch_id": batch_id, "units": len(units)}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.ship_order", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 定向冻结与解除（质检）
    # ------------------------------------------------------------------

    def create_freeze(self, *, request_id: str, actor_id: str, target_type: str, target_id: str,
                      reason: str) -> dict[str, Any]:
        """沿真实用料关系只冻结受影响的库存、订单和渠道批次。"""

        payload = {"actor_id": actor_id, "target_type": target_type,
                   "target_id": target_id, "reason": reason}
        if target_type not in ("material_lot", "component", "finished_unit",
                               "finished_batch", "sales_order", "channel_batch"):
            raise ValidationError("冻结对象类型无效")
        target_id = self._identifier(target_id, "target_id")
        reason = self._text(reason, "reason", 200)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "qc", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                seed_type, seed_id = self._normalize_freeze_seed(connection, target_type, target_id)
                affected = self._collect_downstream(connection, seed_type, seed_id)
                freeze_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO freezes(freeze_id,reason,status,created_by,created_at) VALUES(?,?, 'active',?,?)",
                    (freeze_id, reason, actor_id, self._now()),
                )
                frozen_targets = []
                for node_type in ("material_lot", "component", "finished_unit",
                                  "sales_order", "channel_batch"):
                    table, key_column = STATUS_TABLE[node_type]
                    for node_id in sorted(set(affected[node_type])):
                        row = connection.execute(
                            f"SELECT status FROM {table} WHERE {key_column}=?", (node_id,)
                        ).fetchone()
                        if row is None or row["status"] not in FREEZABLE_STATUS[node_type]:
                            continue
                        connection.execute(
                            "INSERT INTO freeze_targets(freeze_id,target_type,target_id,previous_status) "
                            "VALUES(?,?,?,?)",
                            (freeze_id, node_type, node_id, row["status"]),
                        )
                        connection.execute(
                            f"UPDATE {table} SET status='frozen' WHERE {key_column}=?", (node_id,)
                        )
                        frozen_targets.append({"target_type": node_type, "target_id": node_id,
                                               "previous_status": row["status"]})
                append_event(connection, actor_id=actor_id, action="batch.freeze_created",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"seed_type": seed_type, "seed_id": seed_id, "reason": reason,
                                     "targets": frozen_targets},
                             occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id, "frozen": frozen_targets}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.create_freeze", payload=payload, create=create)

    def _normalize_freeze_seed(self, connection, target_type: str, target_id: str) -> tuple[str, str]:
        if target_type == "material_lot":
            self._lot(connection, target_id)
            return "material_lot", target_id
        if target_type == "component":
            self._component(connection, target_id)
            return "component", target_id
        if target_type == "finished_unit":
            row = connection.execute("SELECT unit_id FROM finished_units WHERE unit_id=?",
                                     (target_id,)).fetchone()
            if row is None:
                raise NotFoundError("成品不存在")
            return "finished_unit", target_id
        if target_type == "finished_batch":
            output = self._output(connection, target_id)
            if output["output_kind"] != "finished_units":
                raise ValidationError("冻结对象必须是包装组合产出")
            return "step_output", target_id
        if target_type == "sales_order":
            row = connection.execute("SELECT sales_order_id FROM sales_orders WHERE sales_order_id=?",
                                     (target_id,)).fetchone()
            if row is None:
                raise NotFoundError("销售订单不存在")
            return "sales_order", target_id
        row = connection.execute("SELECT batch_id FROM channel_batches WHERE batch_id=?",
                                 (target_id,)).fetchone()
        if row is None:
            raise NotFoundError("渠道批次不存在")
        return "channel_batch", target_id

    def _children(self, connection, node_type: str, node_id: str) -> list[tuple[str, str]]:
        """沿真实用料关系的下游邻接。"""

        if node_type == "material_lot":
            rows = connection.execute(
                "SELECT child_id FROM genealogy_edges WHERE parent_type='material_lot' AND parent_id=? "
                "AND child_type='step'", (node_id,)
            ).fetchall()
            return [("step", row["child_id"]) for row in rows]
        if node_type == "step":
            rows = connection.execute("SELECT output_id FROM step_outputs WHERE step_id=?",
                                      (node_id,)).fetchall()
            return [("step_output", row["output_id"]) for row in rows]
        if node_type == "step_output":
            result = [("component", row["component_id"]) for row in connection.execute(
                "SELECT component_id FROM components WHERE output_id=?", (node_id,)).fetchall()]
            result += [("finished_unit", row["unit_id"]) for row in connection.execute(
                "SELECT unit_id FROM finished_units WHERE output_id=?", (node_id,)).fetchall()]
            return result
        if node_type == "component":
            rows = connection.execute(
                "SELECT child_id FROM genealogy_edges WHERE parent_type='component' AND parent_id=? "
                "AND child_type='step_output' AND relation IN ('consume','combine','repack')",
                (node_id,),
            ).fetchall()
            return [("step_output", row["child_id"]) for row in rows]
        if node_type == "finished_unit":
            orders = connection.execute("SELECT sales_order_id FROM sales_order_units WHERE unit_id=?",
                                        (node_id,)).fetchall()
            batches = connection.execute("SELECT batch_id FROM channel_batch_units WHERE unit_id=?",
                                         (node_id,)).fetchall()
            return ([("sales_order", row["sales_order_id"]) for row in orders]
                    + [("channel_batch", row["batch_id"]) for row in batches])
        if node_type == "sales_order":
            units = connection.execute("SELECT unit_id FROM sales_order_units WHERE sales_order_id=?",
                                       (node_id,)).fetchall()
            batches = connection.execute("SELECT batch_id FROM channel_batches WHERE sales_order_id=?",
                                         (node_id,)).fetchall()
            return ([("finished_unit", row["unit_id"]) for row in units]
                    + [("channel_batch", row["batch_id"]) for row in batches])
        if node_type == "channel_batch":
            units = connection.execute("SELECT unit_id FROM channel_batch_units WHERE batch_id=?",
                                       (node_id,)).fetchall()
            row = connection.execute("SELECT sales_order_id FROM channel_batches WHERE batch_id=?",
                                     (node_id,)).fetchone()
            result = [("finished_unit", unit["unit_id"]) for unit in units]
            if row:
                result.append(("sales_order", row["sales_order_id"]))
            return result
        return []

    def _collect_downstream(self, connection, seed_type: str, seed_id: str) -> dict[str, list[str]]:
        collected: dict[str, list[str]] = {
            "material_lot": [], "step": [], "step_output": [], "component": [],
            "finished_unit": [], "sales_order": [], "channel_batch": [],
        }
        visited = set()
        queue = [(seed_type, seed_id)]
        while queue:
            node_type, node_id = queue.pop(0)
            if (node_type, node_id) in visited:
                continue
            visited.add((node_type, node_id))
            if node_type in collected:
                collected[node_type].append(node_id)
            queue.extend(self._children(connection, node_type, node_id))
        return collected

    def lift_freeze(self, *, request_id: str, actor_id: str, freeze_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "freeze_id": freeze_id}
        freeze_id = self._identifier(freeze_id, "freeze_id")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "qc", "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze = connection.execute("SELECT * FROM freezes WHERE freeze_id=?",
                                            (freeze_id,)).fetchone()
                if freeze is None:
                    raise NotFoundError("冻结记录不存在")
                if freeze["status"] != "active":
                    raise ConflictError("冻结已解除")
                restored = []
                targets = connection.execute(
                    "SELECT * FROM freeze_targets WHERE freeze_id=?", (freeze_id,)
                ).fetchall()
                for target in targets:
                    table, key_column = STATUS_TABLE[target["target_type"]]
                    cursor = connection.execute(
                        f"UPDATE {table} SET status=? WHERE {key_column}=? AND status='frozen'",
                        (target["previous_status"], target["target_id"]),
                    )
                    if cursor.rowcount == 1:
                        restored.append({"target_type": target["target_type"],
                                         "target_id": target["target_id"],
                                         "status": target["previous_status"]})
                connection.execute(
                    "UPDATE freezes SET status='lifted', lifted_by=?, lifted_at=? WHERE freeze_id=?",
                    (actor_id, self._now(), freeze_id),
                )
                append_event(connection, actor_id=actor_id, action="batch.freeze_lifted",
                             resource_type="freeze", resource_id=freeze_id,
                             detail={"restored": restored}, occurred_at=self._now())
                return "freeze", freeze_id, {"freeze_id": freeze_id, "restored": restored}

            return self._idempotent(connection, request_id=request_id,
                                    action="batch.lift_freeze", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 守恒核对、反向追溯与正向召回（审计/各角色只读）
    # ------------------------------------------------------------------

    def _reader(self, actor_id: str):
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *READ_ROLES)
        return actor

    def verify_conservation(self, *, actor_id: str, order_id: str) -> dict[str, Any]:
        """核对工单每道工序的投入/退料/合格/报废/返工/在制守恒。"""

        self._reader(actor_id)
        connection = self.database.connection
        order = self._order(connection, order_id)
        steps = []
        balanced = True
        for step in connection.execute(
                "SELECT * FROM process_steps WHERE order_id=? ORDER BY seq", (order_id,)).fetchall():
            ledger = self._ledger(connection, step["step_id"])
            wip = self._wip(ledger)
            step_balanced = wip >= -EPSILON
            balanced = balanced and step_balanced
            steps.append({
                "step_id": step["step_id"], "name": step["name"], "unit": step["unit"],
                "is_packaging": bool(step["is_packaging"]),
                "received": ledger["received"], "returned": ledger["returned"],
                "good": ledger["good"], "scrap": ledger["scrap"], "rework": ledger["rework"],
                "wip": wip, "balanced": step_balanced,
            })
        lots = []
        for row in connection.execute(
                "SELECT lot_id, material_code FROM material_issues WHERE order_id=? GROUP BY lot_id",
                (order_id,)).fetchall():
            lot = self._lot(connection, row["lot_id"])
            issued = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM material_issues WHERE lot_id=?",
                (row["lot_id"],)).fetchone()["total"]
            returned = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM material_returns WHERE lot_id=?",
                (row["lot_id"],)).fetchone()["total"]
            difference = round(lot["quantity_received"] - lot["quantity_on_hand"]
                               - (issued - returned), 6)
            lots.append({"lot_id": row["lot_id"], "material_code": row["material_code"],
                         "quantity_received": lot["quantity_received"],
                         "quantity_on_hand": lot["quantity_on_hand"],
                         "net_issued": round(issued - returned, 6),
                         "difference": difference,
                         "balanced": abs(difference) <= EPSILON})
            balanced = balanced and abs(difference) <= EPSILON
        return {"order_id": order_id, "status": order["status"], "steps": steps,
                "lots": lots, "balanced": balanced}

    def trace_backward(self, *, actor_id: str, target_type: str, target_id: str) -> dict[str, Any]:
        """从任一成品（或组件）反查全部原料来源、证明、检验与放行记录。"""

        self._reader(actor_id)
        if target_type not in ("finished_unit", "component"):
            raise ValidationError("追溯起点必须是成品或组件")
        connection = self.database.connection
        visited: set[tuple[str, str]] = set()
        if target_type == "finished_unit":
            row = connection.execute("SELECT unit_id FROM finished_units WHERE unit_id=?",
                                     (target_id,)).fetchone()
            if row is None:
                raise NotFoundError("成品不存在")
        else:
            self._component(connection, target_id)
        return self._trace_node(connection, target_type, target_id, visited)

    def _trace_node(self, connection, node_type: str, node_id: str,
                    visited: set[tuple[str, str]]) -> dict[str, Any]:
        if (node_type, node_id) in visited:
            return {"type": node_type, "id": node_id, "cycle_ref": True}
        visited.add((node_type, node_id))
        if node_type == "finished_unit":
            row = connection.execute("SELECT * FROM finished_units WHERE unit_id=?",
                                     (node_id,)).fetchone()
            return {
                "type": "finished_unit", "id": node_id, "serial_no": row["serial_no"],
                "product_code": row["product_code"], "status": row["status"],
                "parents": [{"via": {"relation": "packed_in"},
                             "node": self._trace_node(connection, "step_output",
                                                      row["output_id"], visited)}],
            }
        if node_type == "component":
            row = connection.execute("SELECT * FROM components WHERE component_id=?",
                                     (node_id,)).fetchone()
            release = self._release_row(connection, "component", node_id)
            return {
                "type": "component", "id": node_id, "serial_no": row["serial_no"],
                "component_code": row["component_code"], "quantity": row["quantity"],
                "remaining_qty": row["remaining_qty"], "unit": row["unit"], "status": row["status"],
                "inspections": self._inspections_of(connection, "component", node_id),
                "release": dict(release) if release else None,
                "parents": [{"via": {"relation": "produced_by"},
                             "node": self._trace_node(connection, "step_output",
                                                      row["output_id"], visited)}],
            }
        if node_type == "step_output":
            row = connection.execute("SELECT * FROM step_outputs WHERE output_id=?",
                                     (node_id,)).fetchone()
            release = self._release_row(connection, "finished_batch", node_id) \
                if row["output_kind"] == "finished_units" else None
            parents = [{"via": {"relation": "output_of"},
                        "node": self._trace_node(connection, "step", row["step_id"], visited)}]
            for edge in connection.execute(
                    "SELECT * FROM genealogy_edges WHERE child_type='step_output' AND child_id=? "
                    "AND parent_type='component'", (node_id,)).fetchall():
                parents.append({
                    "via": {"relation": edge["relation"], "quantity": edge["quantity"],
                            "unit": edge["unit"]},
                    "node": self._trace_node(connection, "component", edge["parent_id"], visited),
                })
            return {
                "type": "step_output", "id": node_id, "output_kind": row["output_kind"],
                "good_quantity": row["good_quantity"], "scrap_quantity": row["scrap_quantity"],
                "unit": row["unit"],
                "inspections": self._inspections_of(connection, "finished_batch", node_id),
                "release": dict(release) if release else None,
                "parents": parents,
            }
        if node_type == "step":
            row = connection.execute("SELECT * FROM process_steps WHERE step_id=?",
                                     (node_id,)).fetchone()
            ledger = self._ledger(connection, node_id)
            parents = []
            for edge in connection.execute(
                    "SELECT * FROM genealogy_edges WHERE child_type='step' AND child_id=? "
                    "AND parent_type='material_lot'", (node_id,)).fetchall():
                parents.append({
                    "via": {"relation": edge["relation"], "quantity": edge["quantity"],
                            "unit": edge["unit"], "substituted_for": edge["substituted_for"]},
                    "node": self._trace_node(connection, "material_lot", edge["parent_id"], visited),
                })
            return {
                "type": "step", "id": node_id, "name": row["name"], "seq": row["seq"],
                "order_id": row["order_id"], "unit": row["unit"],
                "ledger": {"received": ledger["received"], "returned": ledger["returned"],
                           "good": ledger["good"], "scrap": ledger["scrap"],
                           "rework": ledger["rework"], "wip": self._wip(ledger)},
                "parents": parents,
            }
        # material_lot
        row = connection.execute("SELECT * FROM material_lots WHERE lot_id=?", (node_id,)).fetchone()
        certificates = [dict(cert) for cert in connection.execute(
            "SELECT cert_type,cert_number,issuer,issued_on,file_hash FROM supply_certificates WHERE lot_id=?",
            (node_id,)).fetchall()]
        return {
            "type": "material_lot", "id": node_id, "material_code": row["material_code"],
            "material_name": row["material_name"], "origin": row["origin"],
            "supplier_name": row["supplier_name"], "status": row["status"],
            "quantity_received": row["quantity_received"],
            "quantity_on_hand": row["quantity_on_hand"], "unit": row["unit"],
            "certificates": certificates,
            "inspections": self._inspections_of(connection, "material_lot", node_id),
            "parents": [],
        }

    def recall_scope(self, *, actor_id: str, target_type: str, target_id: str) -> dict[str, Any]:
        """从问题对象正向计算召回范围与数量差异。"""

        self._reader(actor_id)
        connection = self.database.connection
        seed_type, seed_id = self._normalize_freeze_seed(connection, target_type, target_id)
        affected = self._collect_downstream(connection, seed_type, seed_id)
        lots = []
        for lot_id in sorted(set(affected["material_lot"])):
            lot = self._lot(connection, lot_id)
            issued = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM material_issues WHERE lot_id=?",
                (lot_id,)).fetchone()["total"]
            returned = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM material_returns WHERE lot_id=?",
                (lot_id,)).fetchone()["total"]
            lots.append({
                "lot_id": lot_id, "material_code": lot["material_code"], "status": lot["status"],
                "quantity_received": lot["quantity_received"],
                "quantity_on_hand": lot["quantity_on_hand"],
                "net_issued": round(issued - returned, 6),
                "difference": round(lot["quantity_received"] - lot["quantity_on_hand"]
                                    - (issued - returned), 6),
            })
        steps = []
        for step_id in sorted(set(affected["step"])):
            step = self._step(connection, step_id)
            ledger = self._ledger(connection, step_id)
            steps.append({
                "step_id": step_id, "name": step["name"], "order_id": step["order_id"],
                "received": ledger["received"], "returned": ledger["returned"],
                "good": ledger["good"], "scrap": ledger["scrap"], "rework": ledger["rework"],
                "wip": self._wip(ledger),
            })
        def fetch(table: str, key_column: str, columns: str, ids: list[str]) -> list[dict[str, Any]]:
            unique = sorted(set(ids))
            if not unique:
                return []
            placeholders = ",".join("?" for _ in unique)
            rows = connection.execute(
                f"SELECT {columns} FROM {table} WHERE {key_column} IN ({placeholders})",
                tuple(unique),
            ).fetchall()
            return [dict(row) for row in rows]

        components = fetch("components", "component_id",
                           "component_id,serial_no,component_code,quantity,remaining_qty,unit,status",
                           affected["component"])
        units = fetch("finished_units", "unit_id", "unit_id,serial_no,product_code,status",
                      affected["finished_unit"])
        orders = fetch("sales_orders", "sales_order_id", "sales_order_id,channel,status",
                       affected["sales_order"])
        batches = fetch("channel_batches", "batch_id", "batch_id,sales_order_id,channel,status",
                        affected["channel_batch"])
        unit_status: dict[str, int] = {}
        for unit in units:
            unit_status[unit["status"]] = unit_status.get(unit["status"], 0) + 1
        return {
            "seed": {"target_type": target_type, "target_id": target_id},
            "affected": {
                "material_lots": lots,
                "steps": steps,
                "components": components,
                "finished_units": units,
                "sales_orders": orders,
                "channel_batches": batches,
            },
            "quantities": {
                "units_total": len(units),
                "units_by_status": unit_status,
                "units_accounted": sum(unit_status.values()),
                "units_difference": len(units) - sum(unit_status.values()),
                "lots_balanced": all(abs(lot["difference"]) <= EPSILON for lot in lots),
                "steps_balanced": all(step["wip"] >= -EPSILON for step in steps),
            },
        }

    # ------------------------------------------------------------------
    # 状态读取
    # ------------------------------------------------------------------

    def get_lot(self, *, actor_id: str, lot_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        row = self._lot(self.database.connection, lot_id)
        result = dict(row)
        result["certificates"] = [dict(cert) for cert in self.database.connection.execute(
            "SELECT cert_type,cert_number,issuer,issued_on,file_hash FROM supply_certificates WHERE lot_id=?",
            (lot_id,)).fetchall()]
        return result

    def get_component(self, *, actor_id: str, component_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        return dict(self._component(self.database.connection, component_id))

    def get_unit(self, *, actor_id: str, serial_no: str) -> dict[str, Any]:
        self._reader(actor_id)
        row = self.database.connection.execute(
            "SELECT * FROM finished_units WHERE serial_no=?", (serial_no,)).fetchone()
        if row is None:
            raise NotFoundError("成品不存在")
        return dict(row)

    def get_work_order(self, *, actor_id: str, order_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        connection = self.database.connection
        order = dict(self._order(connection, order_id))
        order["steps"] = self.verify_conservation(actor_id=actor_id, order_id=order_id)["steps"]
        return order

    def get_freeze(self, *, actor_id: str, freeze_id: str) -> dict[str, Any]:
        self._reader(actor_id)
        connection = self.database.connection
        freeze = connection.execute("SELECT * FROM freezes WHERE freeze_id=?",
                                    (freeze_id,)).fetchone()
        if freeze is None:
            raise NotFoundError("冻结记录不存在")
        result = dict(freeze)
        result["targets"] = [dict(row) for row in connection.execute(
            "SELECT target_type,target_id,previous_status FROM freeze_targets WHERE freeze_id=?",
            (freeze_id,)).fetchall()]
        return result

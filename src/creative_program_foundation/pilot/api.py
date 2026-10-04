"""试产批次控制系统的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import DomainError, NotFoundError, ValidationError
from .service import PilotService


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    payload = {**(receipt.response or {}),
               "request_id": receipt.request_id,
               "resource_type": receipt.resource_type,
               "resource_id": receipt.resource_id,
               "replayed": receipt.replayed}
    return 200 if receipt.replayed else 201, payload


def route_pilot(service: PilotService, method: str, path: str,
                body: dict[str, Any] | None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 /pilot 前缀下的请求分派到试产控制服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [s for s in parsed.path.split("/") if s]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    body = {**body, "actor_id": actor_id}
    try:
        # ------------------------------------------------------------
        # 主数据
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/suppliers":
            return _receipt(service.register_supplier(**body))
        if method == "POST" and parsed.path == "/pilot/materials":
            return _receipt(service.register_material(**body))
        if method == "POST" and parsed.path == "/pilot/material-batches":
            return _receipt(service.register_material_batch(**body))
        # ------------------------------------------------------------
        # 库存批次
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/lots/receive":
            return _receipt(service.receive_lot(**body))
        if method == "POST" and parsed.path == "/pilot/lots/repack":
            return _receipt(service.repack_lot(**body))
        if method == "POST" and parsed.path == "/pilot/lots/relabel":
            return _receipt(service.relabel_lot(**body))
        if method == "GET" and len(segments) == 3 and segments[0] == "pilot" and segments[1] == "lots":
            return 200, service.get_lot(segments[2])
        if method == "GET" and len(segments) == 4 and segments[:2] == ["pilot", "lots"] \
                and segments[3] == "balance":
            return 200, service.verify_lot_balance(segments[2]).__dict__
        # ------------------------------------------------------------
        # 检验
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/inspection-specs":
            return _receipt(service.register_inspection_spec(**body))
        if method == "POST" and parsed.path == "/pilot/inspections/lot":
            return _receipt(service.inspect_lot(**body))
        if method == "POST" and parsed.path == "/pilot/inspections/component":
            return _receipt(service.inspect_component(**body))
        # ------------------------------------------------------------
        # 配方
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/recipes":
            return _receipt(service.register_recipe(**body))
        if method == "POST" and parsed.path == "/pilot/recipes/activate":
            return _receipt(service.activate_recipe(**body))
        # ------------------------------------------------------------
        # 工单与领退料
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/work-orders":
            return _receipt(service.open_work_order(**body))
        if method == "POST" and parsed.path == "/pilot/work-orders/close":
            return _receipt(service.close_work_order(**body))
        if method == "GET" and len(segments) == 3 and segments[:2] == ["pilot", "work-orders"]:
            return 200, service.get_work_order(segments[2])
        if method == "GET" and len(segments) == 4 and segments[:2] == ["pilot", "work-orders"] \
                and segments[3] == "balance":
            return 200, service.verify_work_order_balance(segments[2])
        if method == "POST" and parsed.path == "/pilot/issues":
            return _receipt(service.issue_material(**body))
        if method == "POST" and parsed.path == "/pilot/returns":
            return _receipt(service.return_material(**body))
        if method == "POST" and parsed.path == "/pilot/wip-scraps":
            return _receipt(service.report_wip_scrap(**body))
        # ------------------------------------------------------------
        # 产出 / 返工 / 放行
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/outputs":
            return _receipt(service.report_output(**body))
        if method == "POST" and parsed.path == "/pilot/reworks/open":
            return _receipt(service.open_rework(**body))
        if method == "POST" and parsed.path == "/pilot/reworks/finish":
            return _receipt(service.finish_rework(**body))
        if method == "POST" and parsed.path == "/pilot/releases/request":
            return _receipt(service.request_release(**body))
        if method == "POST" and parsed.path == "/pilot/releases/approve":
            return _receipt(service.approve_release(**body))
        if method == "POST" and parsed.path == "/pilot/releases/reject":
            return _receipt(service.reject_release(**body))
        if method == "GET" and len(segments) == 3 and segments[:2] == ["pilot", "components"]:
            return 200, service.get_component(segments[2])
        if method == "GET" and len(segments) == 4 and segments[:2] == ["pilot", "components"] \
                and segments[3] == "trace":
            return 200, service.trace_component(segments[2])
        # ------------------------------------------------------------
        # 订单 / 渠道 / 包装
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/orders":
            return _receipt(service.register_order(**body))
        if method == "POST" and parsed.path == "/pilot/channel-batches":
            return _receipt(service.register_channel_batch(**body))
        if method == "POST" and parsed.path == "/pilot/packages":
            return _receipt(service.pack_package(**body))
        # ------------------------------------------------------------
        # 冻结与召回
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/pilot/freezes":
            return _receipt(service.raise_freeze(**body))
        if method == "POST" and parsed.path == "/pilot/freezes/lift":
            return _receipt(service.lift_freeze(**body))
        if method == "GET" and parsed.path == "/pilot/freezes":
            active_only = query.get("active_only", ["0"])[0] in ("1", "true", "yes")
            return 200, {"items": service.list_freeze_events(active_only)}
        if method == "GET" and parsed.path == "/pilot/recall":
            source_kind = query.get("source_kind", [""])[0]
            source_id = query.get("source_id", [""])[0]
            if not source_kind or not source_id:
                raise ValidationError("source_kind 和 source_id 不能为空")
            return 200, service.recall_scope(source_kind=source_kind,
                                            source_id=source_id).__dict__
        raise NotFoundError("试产控制接口不存在")
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}

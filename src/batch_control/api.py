"""批次控制模块的 HTTP/JSON 边界，与基础服务路由组合对外提供。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from creative_program_foundation.api import route as foundation_route
from creative_program_foundation.errors import DomainError, ValidationError
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

from .service import BatchControlService


# POST 路径到服务方法的映射；方法都接收 actor_id 与请求体字段。
POST_ROUTES: dict[str, str] = {
    "/batch/material-lots": "register_material_lot",
    "/batch/inspections": "record_inspection",
    "/batch/releases": "approve_release",
    "/batch/recipes": "create_recipe",
    "/batch/recipes/activate": "activate_recipe",
    "/batch/work-orders": "create_work_order",
    "/batch/issues": "issue_material",
    "/batch/returns": "return_material",
    "/batch/outputs": "report_output",
    "/batch/reworks/resolve": "resolve_rework",
    "/batch/components/rework": "rework_component",
    "/batch/components/rework/resolve": "resolve_component_rework",
    "/batch/unpack": "unpack_output",
    "/batch/sales-orders": "create_sales_order",
    "/batch/shipments": "ship_order",
    "/batch/freezes": "create_freeze",
    "/batch/freezes/lift": "lift_freeze",
}


def route_batch(service: BatchControlService, method: str, path: str,
                body: dict[str, Any] | None, headers: dict[str, str] | None = None
                ) -> tuple[int, dict[str, Any]] | None:
    """处理批次控制接口；路径不属于本模块时返回 None。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path in POST_ROUTES:
            result = getattr(service, POST_ROUTES[parsed.path])(actor_id=actor_id, **body)
            replayed = bool(result.pop("replayed", False))
            return (200 if replayed else 201), result
        if method == "GET" and parsed.path == "/batch/trace":
            query = parse_qs(parsed.query)
            target_type = query.get("target_type", [""])[0]
            target_id = query.get("target_id", [""])[0]
            if not target_type or not target_id:
                raise ValidationError("target_type 与 target_id 不能为空")
            return 200, service.trace_backward(actor_id=actor_id, target_type=target_type,
                                               target_id=target_id)
        if method == "GET" and parsed.path == "/batch/recall":
            query = parse_qs(parsed.query)
            target_type = query.get("target_type", [""])[0]
            target_id = query.get("target_id", [""])[0]
            if not target_type or not target_id:
                raise ValidationError("target_type 与 target_id 不能为空")
            return 200, service.recall_scope(actor_id=actor_id, target_type=target_type,
                                             target_id=target_id)
        if method == "GET" and parsed.path == "/batch/conservation":
            query = parse_qs(parsed.query)
            order_id = query.get("order_id", [""])[0]
            if not order_id:
                raise ValidationError("order_id 不能为空")
            return 200, service.verify_conservation(actor_id=actor_id, order_id=order_id)
        if method == "GET" and parsed.path == "/batch/work-order":
            query = parse_qs(parsed.query)
            order_id = query.get("order_id", [""])[0]
            if not order_id:
                raise ValidationError("order_id 不能为空")
            return 200, service.get_work_order(actor_id=actor_id, order_id=order_id)
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def make_route(domain_service: DomainService,
               batch_service: BatchControlService) -> Callable[..., tuple[int, dict[str, Any]]]:
    """组合批次控制与基础服务的路由函数。"""

    def combined(method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        result = route_batch(batch_service, method, path, body, headers)
        if result is not None:
            return result
        return foundation_route(domain_service, method, path, body, headers)

    return combined


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    route_fn: staticmethod = staticmethod(lambda *args: (500, {"error": "not_configured"}))

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = self.route_fn(self.command, self.path, body,
                                        {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动批次控制 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动茶·道试产批次控制服务")
    parser.add_argument("--database", default="batch_control.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.route_fn = staticmethod(make_route(DomainService(database),
                                               BatchControlService(database)))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

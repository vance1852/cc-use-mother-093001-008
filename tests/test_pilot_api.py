"""试产批次控制 HTTP 路由测试。"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.api import route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.pilot.service import PilotService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


def headers(actor):
    return {"X-Actor-Id": actor}


class PilotApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "api-test.sqlite3")
        clock = FixedClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        self.ds = DomainService(self.database, clock)
        self.ps = PilotService(self.database, clock)
        self.ds.register_organization(request_id="org-req", actor_id="bootstrap",
                                      organization_id="o1", name="品牌机构")
        self.ds.register_actor(request_id="adm-req", actor_id="bootstrap", new_actor_id="adm",
                               display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, role in (("wh", "wh", "warehouse"), ("qc", "qc", "qinspector"),
                               ("fac", "fac", "factory"), ("br", "br", "brand")):
            self.ds.register_actor(request_id=f"a-{rid}", actor_id="adm", new_actor_id=aid,
                                   display_name=role, role=role, organization_id="o1")

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def call(self, method, path, body=None, actor="adm"):
        return route(self.ds, method, path, body or {}, headers(actor),
                     pilot_service=self.ps)

    def test_health_unchanged(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_pilot_route_requires_service_flag(self):
        status, body = route(self.ds, "GET", "/pilot/freezes", None)
        self.assertEqual(404, status)

    def test_role_boundary_over_http(self):
        status, body = self.call("POST", "/pilot/suppliers",
                                 {"request_id": "r1", "supplier_id": "s1", "name": "茶农"},
                                 actor="fac")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

    def test_end_to_end_over_http(self):
        # 仓库：供应商/物料/批次/到货
        self.assertEqual(201, self.call("POST", "/pilot/suppliers",
            {"request_id": "r-sup", "supplier_id": "sup", "name": "福建茶农"}, "wh")[0])
        self.assertEqual(201, self.call("POST", "/pilot/materials",
            {"request_id": "r-tea", "material_id": "tea", "kind": "tea",
             "name": "大红袍", "unit": "kg"}, "wh")[0])
        self.assertEqual(201, self.call("POST", "/pilot/materials",
            {"request_id": "r-gl", "material_id": "glaze", "kind": "glaze",
             "name": "天目釉", "unit": "kg"}, "wh")[0])
        self.assertEqual(201, self.call("POST", "/pilot/material-batches",
            {"request_id": "r-bt", "batch_id": "bt", "material_id": "tea",
             "supplier_id": "sup", "delivery_note": "DN-1", "cert_no": "CERT-1",
             "cert_summary": {"x": "ok"}, "origin_note": "武夷山"}, "wh")[0])
        self.assertEqual(201, self.call("POST", "/pilot/material-batches",
            {"request_id": "r-bg", "batch_id": "bg", "material_id": "glaze",
             "supplier_id": "sup", "delivery_note": "DN-2", "cert_no": "CERT-2",
             "cert_summary": {"x": "ok"}, "origin_note": "景德镇"}, "wh")[0])
        lot_tea = self.call("POST", "/pilot/lots/receive",
                            {"request_id": "r-lt", "batch_id": "bt", "label": "LT",
                             "qty": 20}, "wh")[1]["resource_id"]
        lot_gl = self.call("POST", "/pilot/lots/receive",
                           {"request_id": "r-lg", "batch_id": "bg", "label": "LG",
                            "qty": 10}, "wh")[1]["resource_id"]
        # 质检：规范 + 合格
        self.assertEqual(201, self.call("POST", "/pilot/inspection-specs",
            {"request_id": "r-spt", "spec_id": "spt", "applies_to": "material",
             "ref_id": "tea", "version": 1, "items": ["农残"]}, "qc")[0])
        self.assertEqual(201, self.call("POST", "/pilot/inspection-specs",
            {"request_id": "r-spg", "spec_id": "spg", "applies_to": "material",
             "ref_id": "glaze", "version": 1, "items": ["铅镉"]}, "qc")[0])
        self.assertEqual(201, self.call("POST", "/pilot/inspections/lot",
            {"request_id": "r-it", "lot_id": lot_tea, "spec_id": "spt",
             "decision": "pass"}, "qc")[0])
        self.assertEqual(201, self.call("POST", "/pilot/inspections/lot",
            {"request_id": "r-ig", "lot_id": lot_gl, "spec_id": "spg",
             "decision": "pass"}, "qc")[0])
        # 品牌：配方、组件检验规范、订单/渠道
        self.assertEqual(201, self.call("POST", "/pilot/recipes",
            {"request_id": "r-rc", "recipe_id": "rec", "product_code": "SET", "version": 1,
             "lines": [{"line_seq": 1, "step_seq": 1, "input_kind": "material",
                        "input_ref_id": "tea", "qty_per": 1.0, "allow_substitute": False},
                       {"line_seq": 2, "step_seq": 1, "input_kind": "material",
                        "input_ref_id": "glaze", "qty_per": 0.5, "allow_substitute": False}]},
            "br")[0])
        self.assertEqual(201, self.call("POST", "/pilot/inspection-specs",
            {"request_id": "r-spc", "spec_id": "spc", "applies_to": "component",
             "ref_id": "SET", "version": 1, "items": ["外观"]}, "qc")[0])
        self.assertEqual(201, self.call("POST", "/pilot/orders",
            {"request_id": "r-ord", "order_id": "ord", "channel": "门店",
             "product_code": "SET", "qty": 2}, "br")[0])
        self.assertEqual(201, self.call("POST", "/pilot/channel-batches",
            {"request_id": "r-cb", "batch_id": "cb", "order_id": "ord",
             "qty_planned": 2}, "br")[0])
        # 工厂：工单；仓库：发料
        self.assertEqual(201, self.call("POST", "/pilot/work-orders",
            {"request_id": "r-wo", "wo_id": "wo", "recipe_id": "rec",
             "planned_qty": 2}, "fac")[0])
        self.assertEqual(201, self.call("POST", "/pilot/issues",
            {"request_id": "r-i1", "wo_id": "wo", "line_seq": 1, "lot_id": lot_tea,
             "qty": 2, "scan_ref": "API-1"}, "wh")[0])
        self.assertEqual(201, self.call("POST", "/pilot/issues",
            {"request_id": "r-i2", "wo_id": "wo", "line_seq": 2, "lot_id": lot_gl,
             "qty": 1, "scan_ref": "API-2"}, "wh")[0])
        # 重复扫码不同请求 → 409
        status, body = self.call("POST", "/pilot/issues",
            {"request_id": "r-i-dup", "wo_id": "wo", "line_seq": 1, "lot_id": lot_tea,
             "qty": 2, "scan_ref": "API-1"}, "wh")
        self.assertEqual(409, status)
        # 工厂：产出（2 合格）
        status, out = self.call("POST", "/pilot/outputs",
            {"request_id": "r-o1", "wo_id": "wo", "step_seq": 1,
             "qty_good": 2, "qty_scrap": 0, "qty_rework": 0,
             "materials": [{"line_seq": 1, "lot_id": lot_tea, "qty": 2},
                           {"line_seq": 2, "lot_id": lot_gl, "qty": 1}],
             "material_usage": [{"1": lot_tea, "2": lot_gl},
                                {"1": lot_tea, "2": lot_gl}]}, "fac")
        self.assertEqual(201, status)
        components = out["components"]
        # 质检检验 + 双人放行
        for index, cid in enumerate(components):
            self.assertEqual(201, self.call("POST", "/pilot/inspections/component",
                {"request_id": f"r-ic-{index}", "component_id": cid, "spec_id": "spc",
                 "decision": "pass"}, "qc")[0])
            ticket = self.call("POST", "/pilot/releases/request",
                               {"request_id": f"r-rr-{index}", "component_id": cid}, "fac")[1]
            self.assertEqual(201, self.call("POST", "/pilot/releases/approve",
                {"request_id": f"r-aq-{index}", "ticket_id": ticket["resource_id"],
                 "role": "qinspector"}, "qc")[0])
            self.assertEqual(201, self.call("POST", "/pilot/releases/approve",
                {"request_id": f"r-ab-{index}", "ticket_id": ticket["resource_id"],
                 "role": "brand"}, "br")[0])
        # 品牌包装
        status, pack = self.call("POST", "/pilot/packages",
            {"request_id": "r-pk", "channel_batch_id": "cb",
             "component_ids": components}, "br")
        self.assertEqual(201, status)
        # 只读：守恒 / 追溯 / 召回
        status, balance = self.call("GET", "/pilot/work-orders/wo/balance")
        self.assertEqual(200, status)
        self.assertTrue(balance["balanced"])
        status, trace = self.call("GET", f"/pilot/components/{components[0]}/trace")
        self.assertEqual(200, status)
        self.assertEqual(1, len(trace["packages"]))
        status, recall = self.call(
            "GET", f"/pilot/recall?source_kind=lot&source_id={lot_tea}")
        self.assertEqual(200, status)
        self.assertEqual(2, len(recall["components"]))
        # 污染冻结只沿真实关系
        status, freeze = self.call("POST", "/pilot/freezes",
            {"request_id": "r-fz", "source_kind": "lot", "source_id": lot_tea,
             "reason": "contamination", "note": "农残"}, "qc")
        self.assertEqual(201, status)
        status, events = self.call("GET", "/pilot/freezes?active_only=1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(events["items"]))
        # 无效 JSON 形状 → 400
        status, body = self.call("POST", "/pilot/suppliers",
                                 {"request_id": "bad"}, "wh")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])


if __name__ == "__main__":
    unittest.main()

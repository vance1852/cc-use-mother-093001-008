"""试产批次控制系统的 SQLite 表结构。"""

from __future__ import annotations

from ..storage import Database  # noqa: F401  (复用连接与事务边界)

PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('tea','glaze','packaging','other')),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_batches (
    batch_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    delivery_note TEXT NOT NULL,
    cert_no TEXT NOT NULL,
    cert_summary_json TEXT NOT NULL,
    origin_note TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_lots (
    lot_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES material_batches(batch_id),
    label TEXT NOT NULL,
    qty_received REAL NOT NULL CHECK(qty_received >= 0),
    remaining_qty REAL NOT NULL CHECK(remaining_qty >= 0),
    status TEXT NOT NULL CHECK(status IN ('quarantine','available','frozen','closed')),
    pre_freeze_status TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_lineage (
    lineage_id TEXT PRIMARY KEY,
    parent_lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    child_lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    reason TEXT NOT NULL CHECK(reason IN ('split','merge')),
    qty REAL NOT NULL CHECK(qty > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_relabels (
    relabel_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    old_label TEXT NOT NULL,
    new_label TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspection_specs (
    spec_id TEXT PRIMARY KEY,
    applies_to TEXT NOT NULL CHECK(applies_to IN ('material','component')),
    ref_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    items_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(applies_to, ref_id, version)
);
CREATE TABLE IF NOT EXISTS inspection_results (
    result_id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL REFERENCES inspection_specs(spec_id),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('lot','component')),
    target_id TEXT NOT NULL,
    is_additional INTEGER NOT NULL CHECK(is_additional IN (0,1)),
    decision TEXT NOT NULL CHECK(decision IN ('pass','fail')),
    inspector_id TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id TEXT PRIMARY KEY,
    product_code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_code, version)
);
CREATE TABLE IF NOT EXISTS recipe_lines (
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
    line_seq INTEGER NOT NULL CHECK(line_seq >= 1),
    step_seq INTEGER NOT NULL CHECK(step_seq >= 1),
    input_kind TEXT NOT NULL CHECK(input_kind IN ('material','product')),
    input_ref_id TEXT NOT NULL,
    qty_per REAL NOT NULL CHECK(qty_per > 0),
    allow_substitute INTEGER NOT NULL CHECK(allow_substitute IN (0,1)),
    PRIMARY KEY(recipe_id, line_seq)
);
CREATE TABLE IF NOT EXISTS work_orders (
    wo_id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
    product_code TEXT NOT NULL,
    recipe_version INTEGER NOT NULL,
    planned_qty REAL NOT NULL CHECK(planned_qty > 0),
    status TEXT NOT NULL CHECK(status IN ('open','in_progress','completed','closed','frozen')),
    pre_freeze_status TEXT,
    opened_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS kit_allocations (
    wo_id TEXT NOT NULL REFERENCES work_orders(wo_id),
    line_seq INTEGER NOT NULL,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    substitute_for_material_id TEXT,
    allocated_qty REAL NOT NULL CHECK(allocated_qty > 0),
    PRIMARY KEY(wo_id, line_seq, lot_id)
);
CREATE TABLE IF NOT EXISTS material_transactions (
    txn_id TEXT PRIMARY KEY,
    wo_id TEXT,
    line_seq INTEGER,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    kind TEXT NOT NULL CHECK(kind IN ('receive','issue','return','scrap','wip_scrap','consume','repack_out','repack_in')),
    qty_delta REAL NOT NULL CHECK(qty_delta <> 0),
    ref_type TEXT,
    ref_id TEXT,
    scan_ref TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mt_scan_guard
    ON material_transactions(scan_ref)
    WHERE scan_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS material_consumption (
    output_id TEXT NOT NULL REFERENCES process_outputs(output_id),
    line_seq INTEGER NOT NULL,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    qty REAL NOT NULL CHECK(qty > 0),
    PRIMARY KEY(output_id, line_seq, lot_id)
);
-- 组件级用料归属：当同一配方行由多个真实批次（含替代料）供料时，
-- 精确记录每件产出分别消耗了哪个批次；单批次时自动全量归属。
-- 精准召回/冻结沿这张表定位，不波及无关组件。
CREATE TABLE IF NOT EXISTS output_component_lots (
    component_id TEXT NOT NULL REFERENCES components(component_id),
    line_seq INTEGER NOT NULL,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    qty REAL NOT NULL CHECK(qty >= 0),
    PRIMARY KEY(component_id, line_seq, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_ocl_lot ON output_component_lots(lot_id);
-- 跨工序真实用料关系：每个组件由上一工序哪些组件装配/加工而来
CREATE TABLE IF NOT EXISTS component_inputs (
    component_id TEXT NOT NULL REFERENCES components(component_id),
    input_component_id TEXT NOT NULL REFERENCES components(component_id),
    step_seq INTEGER NOT NULL,
    PRIMARY KEY(component_id, input_component_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_input ON component_inputs(input_component_id);
CREATE TABLE IF NOT EXISTS process_outputs (
    output_id TEXT PRIMARY KEY,
    wo_id TEXT NOT NULL REFERENCES work_orders(wo_id),
    step_seq INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'normal' CHECK(kind IN ('normal','rework')),
    qty_total REAL NOT NULL CHECK(qty_total > 0),
    qty_good REAL NOT NULL CHECK(qty_good >= 0),
    qty_scrap REAL NOT NULL CHECK(qty_scrap >= 0),
    qty_rework REAL NOT NULL CHECK(qty_rework >= 0),
    produced_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_po_wo_step ON process_outputs(wo_id, step_seq);
CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    product_code TEXT NOT NULL,
    output_id TEXT REFERENCES process_outputs(output_id),
    wo_id TEXT NOT NULL REFERENCES work_orders(wo_id),
    step_seq INTEGER NOT NULL,
    serial_no TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_inspection','released','scrapped','reworked','blocked','consumed')),
    frozen INTEGER NOT NULL CHECK(frozen IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(wo_id, serial_no)
);
CREATE TABLE IF NOT EXISTS component_relations (
    relation_id TEXT PRIMARY KEY,
    parent_component_id TEXT NOT NULL REFERENCES components(component_id),
    child_component_id TEXT NOT NULL REFERENCES components(component_id),
    relation TEXT NOT NULL CHECK(relation IN ('assembled_from','reworked_from')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cr_child ON component_relations(child_component_id);
CREATE INDEX IF NOT EXISTS idx_cr_parent ON component_relations(parent_component_id);
CREATE TABLE IF NOT EXISTS rework_jobs (
    rework_id TEXT PRIMARY KEY,
    wo_id TEXT NOT NULL REFERENCES work_orders(wo_id),
    step_seq INTEGER NOT NULL,
    source_component_id TEXT NOT NULL REFERENCES components(component_id),
    result_component_id TEXT REFERENCES components(component_id),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS release_tickets (
    ticket_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
    recipe_version INTEGER NOT NULL,
    inspection_result_id TEXT NOT NULL REFERENCES inspection_results(result_id),
    status TEXT NOT NULL CHECK(status IN ('pending','released','rejected')),
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS release_approvals (
    ticket_id TEXT NOT NULL REFERENCES release_tickets(ticket_id),
    role TEXT NOT NULL CHECK(role IN ('qinspector','brand')),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticket_id, role)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    product_code TEXT NOT NULL,
    qty REAL NOT NULL CHECK(qty > 0),
    status TEXT NOT NULL CHECK(status IN ('open','frozen')),
    pre_freeze_status TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_batches (
    batch_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    qty_planned REAL NOT NULL CHECK(qty_planned > 0),
    status TEXT NOT NULL CHECK(status IN ('open','sealed','frozen')),
    pre_freeze_status TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_at TEXT
);
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    channel_batch_id TEXT NOT NULL REFERENCES channel_batches(batch_id),
    wo_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('sealed','frozen')),
    pre_freeze_status TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_items (
    package_id TEXT NOT NULL REFERENCES packages(package_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    PRIMARY KEY(package_id, component_id),
    UNIQUE(component_id)
);
CREATE TABLE IF NOT EXISTS freeze_events (
    event_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('lot','component')),
    source_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('contamination','mislabel','inspection_fail')),
    note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','lifted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT,
    lifted_by TEXT
);
CREATE TABLE IF NOT EXISTS freeze_impacts (
    event_id TEXT NOT NULL REFERENCES freeze_events(event_id),
    entity_type TEXT NOT NULL CHECK(entity_type IN ('lot','wo','output','component','package','channel_batch','order')),
    entity_id TEXT NOT NULL,
    PRIMARY KEY(event_id, entity_type, entity_id)
);
"""


def ensure_pilot_schema(database: Database) -> None:
    """在共享数据库上创建试产控制所需的全部表。"""

    database.connection.executescript(PILOT_SCHEMA)

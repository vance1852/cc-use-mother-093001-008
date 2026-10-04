"""批次控制模块在共享 SQLite 上扩展的表结构。"""

from __future__ import annotations

from creative_program_foundation.storage import Database


SCHEMA = """
CREATE TABLE IF NOT EXISTS material_lots (
    lot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    material_code TEXT NOT NULL,
    material_name TEXT NOT NULL,
    origin TEXT NOT NULL,
    supplier_name TEXT NOT NULL,
    unit TEXT NOT NULL,
    quantity_received REAL NOT NULL CHECK(quantity_received > 0),
    quantity_on_hand REAL NOT NULL CHECK(quantity_on_hand >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending_inspection','available','quarantined','frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_certificates (
    cert_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    cert_type TEXT NOT NULL,
    cert_number TEXT NOT NULL,
    issuer TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(lot_id, cert_type, cert_number)
);
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id TEXT PRIMARY KEY,
    product_code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_code, version)
);
CREATE TABLE IF NOT EXISTS recipe_lines (
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
    line_no INTEGER NOT NULL,
    material_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    substitutes_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(recipe_id, line_no)
);
CREATE TABLE IF NOT EXISTS work_orders (
    order_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    product_code TEXT NOT NULL,
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
    recipe_version INTEGER NOT NULL,
    planned_quantity REAL NOT NULL CHECK(planned_quantity > 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','in_progress','completed','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS process_steps (
    step_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    is_packaging INTEGER NOT NULL CHECK(is_packaging IN (0, 1)),
    UNIQUE(order_id, seq)
);
CREATE TABLE IF NOT EXISTS step_ledgers (
    step_id TEXT PRIMARY KEY REFERENCES process_steps(step_id),
    order_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    received REAL NOT NULL DEFAULT 0 CHECK(received >= 0),
    returned REAL NOT NULL DEFAULT 0 CHECK(returned >= 0),
    good REAL NOT NULL DEFAULT 0 CHECK(good >= 0),
    scrap REAL NOT NULL DEFAULT 0 CHECK(scrap >= 0),
    rework REAL NOT NULL DEFAULT 0 CHECK(rework >= 0)
);
CREATE TABLE IF NOT EXISTS material_issues (
    issue_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    step_id TEXT NOT NULL REFERENCES process_steps(step_id),
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    material_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('normal','substitute')),
    substituted_for TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_returns (
    return_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    step_id TEXT NOT NULL REFERENCES process_steps(step_id),
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_outputs (
    output_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    step_id TEXT NOT NULL REFERENCES process_steps(step_id),
    output_kind TEXT NOT NULL CHECK(output_kind IN ('components','finished_units')),
    good_quantity REAL NOT NULL CHECK(good_quantity >= 0),
    scrap_quantity REAL NOT NULL DEFAULT 0 CHECK(scrap_quantity >= 0),
    unit TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    serial_no TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    step_id TEXT NOT NULL REFERENCES process_steps(step_id),
    output_id TEXT NOT NULL REFERENCES step_outputs(output_id),
    component_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    remaining_qty REAL NOT NULL CHECK(remaining_qty >= 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('wip','released','consumed','rework','scrapped','quarantined','frozen')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finished_units (
    unit_id TEXT PRIMARY KEY,
    serial_no TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL REFERENCES work_orders(order_id),
    output_id TEXT NOT NULL REFERENCES step_outputs(output_id),
    product_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_stock','allocated','shipped','unpacked','quarantined','frozen','recalled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS genealogy_edges (
    edge_id TEXT PRIMARY KEY,
    parent_type TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    child_type TEXT NOT NULL,
    child_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('consume','substitute','combine','repack','split','rework')),
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '',
    substituted_for TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('material_lot','component','finished_batch')),
    target_id TEXT NOT NULL,
    item TEXT NOT NULL,
    method TEXT NOT NULL,
    standard TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    kind TEXT NOT NULL CHECK(kind IN ('initial','additional')),
    result TEXT NOT NULL CHECK(result IN ('pending','pass','fail')),
    measured_value TEXT NOT NULL DEFAULT '',
    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0, 1)),
    inspector_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('component','finished_batch')),
    target_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_first','released','superseded')),
    first_actor TEXT NOT NULL,
    first_at TEXT NOT NULL,
    second_actor TEXT,
    second_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales_orders (
    sales_order_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed','shipped','frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales_order_units (
    sales_order_id TEXT NOT NULL REFERENCES sales_orders(sales_order_id),
    unit_id TEXT NOT NULL REFERENCES finished_units(unit_id),
    PRIMARY KEY(sales_order_id, unit_id)
);
CREATE TABLE IF NOT EXISTS channel_batches (
    batch_id TEXT PRIMARY KEY,
    sales_order_id TEXT NOT NULL REFERENCES sales_orders(sales_order_id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('shipped','frozen','recalled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_batch_units (
    batch_id TEXT NOT NULL REFERENCES channel_batches(batch_id),
    unit_id TEXT NOT NULL REFERENCES finished_units(unit_id),
    PRIMARY KEY(batch_id, unit_id)
);
CREATE TABLE IF NOT EXISTS freezes (
    freeze_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','lifted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_by TEXT,
    lifted_at TEXT
);
CREATE TABLE IF NOT EXISTS freeze_targets (
    freeze_id TEXT NOT NULL REFERENCES freezes(freeze_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    PRIMARY KEY(freeze_id, target_type, target_id)
);
"""


def ensure_schema(database: Database) -> None:
    """在共享数据库连接上建立批次控制表。"""

    database.connection.executescript(SCHEMA)

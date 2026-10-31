"""
Database schema (table definitions).

Design notes:
- Every business record lives in SQLite, never only in Streamlit memory.
- CHECK constraints stop impossible values (negative stock, unknown statuses).
- UNIQUE constraints stop duplicates (e.g. approving the same plan twice).
- "reserved" columns track stock promised to orders, so the same unit
  can never be allocated to two orders.
"""

from database.connection import get_connection

SCHEMA_SQL = """
-- Catalogue of component types the company buys.
CREATE TABLE IF NOT EXISTS components (
    id            INTEGER PRIMARY KEY,
    part_number   TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    description   TEXT
);

-- Fictional suppliers. `approved` = passed TechNova's vendor qualification.
-- `rfq_behavior` controls how the simulated supplier answers quotation requests.
CREATE TABLE IF NOT EXISTS suppliers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    country       TEXT NOT NULL,
    reliability   REAL NOT NULL CHECK (reliability BETWEEN 0 AND 1),
    approved      INTEGER NOT NULL CHECK (approved IN (0, 1)),
    contact_email TEXT,
    rfq_behavior  TEXT NOT NULL DEFAULT 'QUOTE' CHECK (rfq_behavior IN ('QUOTE', 'DECLINE', 'NO_RESPONSE')),
    quote_note    TEXT  -- free text the supplier attaches to quotes (UNTRUSTED data)
);

-- What each supplier can sell. Free stock = stock_qty - reserved_qty.
CREATE TABLE IF NOT EXISTS supplier_inventory (
    id             INTEGER PRIMARY KEY,
    supplier_id    INTEGER NOT NULL REFERENCES suppliers(id),
    part_number    TEXT NOT NULL REFERENCES components(part_number),
    stock_qty      INTEGER NOT NULL CHECK (stock_qty >= 0),
    reserved_qty   INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    unit_price     REAL NOT NULL CHECK (unit_price > 0),
    lead_time_days INTEGER NOT NULL CHECK (lead_time_days >= 0),
    UNIQUE (supplier_id, part_number),
    CHECK (reserved_qty <= stock_qty)
);

-- TechNova's own warehouse. Free stock = quantity_on_hand - quantity_reserved.
CREATE TABLE IF NOT EXISTS warehouse_inventory (
    id                INTEGER PRIMARY KEY,
    part_number       TEXT NOT NULL UNIQUE REFERENCES components(part_number),
    location          TEXT NOT NULL,
    quantity_on_hand  INTEGER NOT NULL CHECK (quantity_on_hand >= 0),
    quantity_reserved INTEGER NOT NULL DEFAULT 0 CHECK (quantity_reserved >= 0),
    CHECK (quantity_reserved <= quantity_on_hand)
);

-- A requirement entered by a procurement officer.
CREATE TABLE IF NOT EXISTS procurement_requests (
    id                   INTEGER PRIMARY KEY,
    component_name       TEXT NOT NULL,
    part_number          TEXT NOT NULL,
    quantity             INTEGER NOT NULL CHECK (quantity > 0),
    required_by          TEXT NOT NULL,          -- YYYY-MM-DD
    max_budget           REAL NOT NULL CHECK (max_budget > 0),
    quality_requirements TEXT,
    status               TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN
                         ('NEW', 'PLANNING', 'PLAN_PROPOSED', 'ORDERED', 'REJECTED', 'INFEASIBLE', 'FAILED')),
    scenario             TEXT,
    parent_request_id    INTEGER REFERENCES procurement_requests(id),
    created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Request For Quotation sent (in simulation) to one supplier.
CREATE TABLE IF NOT EXISTS rfqs (
    id            INTEGER PRIMARY KEY,
    request_id    INTEGER NOT NULL REFERENCES procurement_requests(id),
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
    part_number   TEXT NOT NULL,
    requested_qty INTEGER NOT NULL CHECK (requested_qty > 0),
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'RECEIVED', 'DECLINED')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    responded_at  TEXT,
    UNIQUE (request_id, supplier_id)
);

-- The (simulated) supplier's answer to an RFQ.
CREATE TABLE IF NOT EXISTS quotations (
    id               INTEGER PRIMARY KEY,
    rfq_id           INTEGER NOT NULL UNIQUE REFERENCES rfqs(id),
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id),
    part_number      TEXT NOT NULL,
    offered_qty      INTEGER NOT NULL CHECK (offered_qty >= 0),
    unit_price       REAL NOT NULL CHECK (unit_price > 0),
    lead_time_days   INTEGER NOT NULL CHECK (lead_time_days >= 0),
    valid_until      TEXT NOT NULL,
    supplier_message TEXT,              -- UNTRUSTED: shown to humans, never obeyed
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- One execution of the LangGraph agent.
CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY,
    run_type      TEXT NOT NULL CHECK (run_type IN ('PROCUREMENT', 'RECOVERY')),
    request_id    INTEGER REFERENCES procurement_requests(id),
    disruption_id INTEGER,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    iterations    INTEGER NOT NULL DEFAULT 0,
    llm_used      INTEGER NOT NULL DEFAULT 1,
    final_summary TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at   TEXT
);

-- A plan proposed by the agent (procurement or recovery). Needs human approval.
CREATE TABLE IF NOT EXISTS procurement_plans (
    id                INTEGER PRIMARY KEY,
    plan_type         TEXT NOT NULL CHECK (plan_type IN ('PROCUREMENT', 'RECOVERY')),
    request_id        INTEGER NOT NULL REFERENCES procurement_requests(id),
    disruption_id     INTEGER,
    agent_run_id      INTEGER REFERENCES agent_runs(id),
    status            TEXT NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'INFEASIBLE', 'SUPERSEDED')),
    feasible          INTEGER NOT NULL CHECK (feasible IN (0, 1)),
    quantity_needed   INTEGER NOT NULL CHECK (quantity_needed >= 0),
    quantity_covered  INTEGER NOT NULL CHECK (quantity_covered >= 0),
    total_cost        REAL NOT NULL CHECK (total_cost >= 0),
    budget_available  REAL NOT NULL,    -- budget the plan was checked against
    required_by       TEXT,             -- deadline the plan was checked against
    expected_delivery TEXT,
    risk_level        TEXT,
    risk_notes        TEXT,
    reasons           TEXT,             -- JSON list of validation / infeasibility reasons
    ai_explanation    TEXT,             -- LLM-written summary (numbers in this table are authoritative)
    cost_difference   REAL,             -- recovery only: cost change vs the disrupted commitment
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    decided_at        TEXT
);

-- One line of a plan: take N units from the warehouse or from a supplier.
CREATE TABLE IF NOT EXISTS plan_items (
    id             INTEGER PRIMARY KEY,
    plan_id        INTEGER NOT NULL REFERENCES procurement_plans(id),
    source_type    TEXT NOT NULL CHECK (source_type IN ('WAREHOUSE', 'SUPPLIER')),
    supplier_id    INTEGER REFERENCES suppliers(id),
    part_number    TEXT NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    unit_price     REAL NOT NULL CHECK (unit_price >= 0),
    line_cost      REAL NOT NULL CHECK (line_cost >= 0),
    lead_time_days INTEGER NOT NULL CHECK (lead_time_days >= 0),
    reliability    REAL
);

-- A confirmed (simulated) purchase order. One per procurement request.
CREATE TABLE IF NOT EXISTS purchase_orders (
    id         INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL UNIQUE REFERENCES procurement_requests(id),
    plan_id    INTEGER NOT NULL UNIQUE REFERENCES procurement_plans(id),
    status     TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'AT_RISK', 'DELIVERED', 'CANCELLED')),
    total_cost REAL NOT NULL CHECK (total_cost >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Lines of a purchase order (warehouse allocation or supplier purchase).
CREATE TABLE IF NOT EXISTS order_items (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES purchase_orders(id),
    plan_item_id INTEGER UNIQUE REFERENCES plan_items(id),  -- a plan line can be ordered only once
    source_type  TEXT NOT NULL CHECK (source_type IN ('WAREHOUSE', 'SUPPLIER')),
    supplier_id  INTEGER REFERENCES suppliers(id),
    part_number  TEXT NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   REAL NOT NULL CHECK (unit_price >= 0),
    added_by     TEXT NOT NULL DEFAULT 'PROCUREMENT' CHECK (added_by IN ('PROCUREMENT', 'RECOVERY'))
);

-- What a supplier has promised to deliver for an order line.
-- original_qty never changes; committed_qty is the supplier's latest promise.
CREATE TABLE IF NOT EXISTS supplier_commitments (
    id                     INTEGER PRIMARY KEY,
    order_item_id          INTEGER NOT NULL UNIQUE REFERENCES order_items(id),
    order_id               INTEGER NOT NULL REFERENCES purchase_orders(id),
    supplier_id            INTEGER NOT NULL REFERENCES suppliers(id),
    part_number            TEXT NOT NULL,
    original_qty           INTEGER NOT NULL CHECK (original_qty > 0),
    committed_qty          INTEGER NOT NULL CHECK (committed_qty >= 0),
    unit_price             REAL NOT NULL CHECK (unit_price > 0),
    expected_delivery_date TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN
                           ('CONFIRMED', 'REDUCED', 'DELAYED', 'PRICE_CHANGED', 'CANCELLED', 'REPLACED', 'DELIVERED')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (committed_qty <= original_qty)
);

-- Inbound supplier signals (simulated). The disruption monitor reads these.
CREATE TABLE IF NOT EXISTS supplier_updates (
    id             INTEGER PRIMARY KEY,
    commitment_id  INTEGER NOT NULL REFERENCES supplier_commitments(id),
    update_type    TEXT NOT NULL CHECK (update_type IN ('QUANTITY_CHANGE', 'CANCELLATION', 'DATE_CHANGE', 'PRICE_CHANGE')),
    new_quantity   INTEGER CHECK (new_quantity >= 0),
    new_date       TEXT,
    new_unit_price REAL CHECK (new_unit_price > 0),
    message        TEXT,           -- UNTRUSTED supplier text
    processed      INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1)),
    received_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- A detected supply risk.
CREATE TABLE IF NOT EXISTS disruptions (
    id                 INTEGER PRIMARY KEY,
    supplier_update_id INTEGER UNIQUE REFERENCES supplier_updates(id),  -- one disruption per signal
    commitment_id      INTEGER NOT NULL REFERENCES supplier_commitments(id),
    order_id           INTEGER NOT NULL REFERENCES purchase_orders(id),
    disruption_type    TEXT NOT NULL CHECK (disruption_type IN ('QUANTITY_REDUCED', 'CANCELLED', 'DELAYED', 'PRICE_INCREASE')),
    original_qty       INTEGER NOT NULL,
    updated_qty        INTEGER NOT NULL,
    shortage_qty       INTEGER NOT NULL CHECK (shortage_qty >= 0),
    original_date      TEXT,
    updated_date       TEXT,
    original_price     REAL,
    updated_price      REAL,
    severity           TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH')),
    status             TEXT NOT NULL CHECK (status IN ('OPEN', 'RECOVERY_PROPOSED', 'RESOLVED', 'UNRESOLVED', 'NO_ACTION_NEEDED')),
    details            TEXT,
    detected_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at        TEXT
);

-- Human decisions. UNIQUE(plan_id) = a plan can only be decided once.
CREATE TABLE IF NOT EXISTS approval_events (
    id         INTEGER PRIMARY KEY,
    plan_id    INTEGER NOT NULL UNIQUE REFERENCES procurement_plans(id),
    decision   TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    decided_by TEXT NOT NULL,
    comment    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Timeline of what the agent ACTUALLY did (written as each step executes).
CREATE TABLE IF NOT EXISTS agent_activity_logs (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES agent_runs(id),
    step_number    INTEGER NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN
                   ('REQUIREMENT', 'LLM_DECISION', 'TOOL_CALL', 'VALIDATION', 'RECOMMENDATION',
                    'APPROVAL_REQUEST', 'FALLBACK', 'ERROR')),
    title          TEXT NOT NULL,
    tool_name      TEXT,
    input_summary  TEXT,
    result_summary TEXT,
    status         TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'INFO', 'WARNING')),
    explanation    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# Children first, so dropping never violates a foreign key.
ALL_TABLES = [
    "agent_activity_logs", "approval_events", "disruptions", "supplier_updates",
    "supplier_commitments", "order_items", "purchase_orders", "plan_items", "procurement_plans",
    "agent_runs", "quotations", "rfqs", "procurement_requests", "warehouse_inventory",
    "supplier_inventory", "suppliers", "components",
]


def create_schema() -> None:
    """Create all tables (safe to call repeatedly)."""
    connection = get_connection()
    try:
        connection.executescript(SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()


def drop_all_tables() -> None:
    """Delete every table. Used only by the 'Reset Demo' function."""
    connection = get_connection()
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in ALL_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
    finally:
        connection.close()


def database_is_initialized() -> bool:
    """Return True when the core tables exist and contain suppliers."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'suppliers'"
        ).fetchone()
        if row is None:
            return False
        count = connection.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
        return count > 0
    finally:
        connection.close()

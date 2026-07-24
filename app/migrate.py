"""Additive-only SQLite migrations, run at startup before seeds."""
from sqlalchemy import bindparam, text

from app import addresses


def _dedupe_and_unique_visits(conn) -> None:
    """A property has at most one visit per calendar date. The old /week/move
    path could leave a duplicate 'due' row (double-billing risk). Collapse any
    existing duplicates — keeping the most-complete row (done > skipped > due,
    then a real completed_at, then newest id) — and add a UNIQUE index so the
    invariant is enforced from here on (the get-or-create/move paths catch the
    IntegrityError and re-select)."""
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(visits)"))}
    if not {"id", "property_id", "date", "status"} <= cols:
        return
    groups = conn.execute(text(
        "SELECT property_id, date FROM visits GROUP BY property_id, date HAVING COUNT(*) > 1")).all()
    for property_id, d in groups:
        rows = conn.execute(text(
            "SELECT id, status, completed_at FROM visits WHERE property_id=:p AND date=:d"),
            {"p": property_id, "d": d}).all()

        def rank(r):  # higher = keep
            return ({"done": 2, "skipped": 1}.get(r[1], 0), 1 if r[2] is not None else 0, r[0])

        losers = [r[0] for r in sorted(rows, key=rank, reverse=True)[1:]]
        if losers:
            conn.execute(text("DELETE FROM visit_chemicals WHERE visit_id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": losers})
            conn.execute(text("DELETE FROM visits WHERE id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": losers})
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_visits_property_date ON visits (property_id, date)"))

# Indexes on hot, frequently-filtered columns. `Base.metadata.create_all` only
# creates these on brand-new tables, so an already-provisioned database needs
# them added explicitly. CREATE INDEX IF NOT EXISTS is idempotent and cheap.
INDEXES = [
    ("ix_visits_property_id", "visits", "property_id"),
    ("ix_visits_date", "visits", "date"),
    ("ix_visits_status", "visits", "status"),
    ("ix_visits_original_date", "visits", "original_date"),
    ("ix_photos_entity", "photos", "entity_type, entity_id"),
    ("ix_jobs_status", "jobs", "status"),
    ("ix_audit_log_at", "audit_log", "at"),
]

COLUMNS = {
    "users": [
        ("username", "VARCHAR(40)"),
        ("active", "BOOLEAN DEFAULT 1"),
        ("view_money", "BOOLEAN DEFAULT 0"),
        ("manage_customers", "BOOLEAN DEFAULT 0"),
        ("can_repairs", "BOOLEAN DEFAULT 0"),
        ("view_all_routes", "BOOLEAN DEFAULT 0"),
        ("mark_cash", "BOOLEAN DEFAULT 0"),
        ("totp_secret", "VARCHAR(200)"),
        ("totp_enabled", "BOOLEAN DEFAULT 0"),
        ("session_epoch", "INTEGER DEFAULT 0"),
    ],
    "properties": [
        ("assigned_tech_id", "INTEGER REFERENCES users(id)"),
        ("spa_rate_override_cents", "INTEGER"),
        ("street", "VARCHAR(120) DEFAULT ''"),
        ("city", "VARCHAR(80) DEFAULT ''"),
        ("state", "VARCHAR(20) DEFAULT ''"),
        ("zip", "VARCHAR(16) DEFAULT ''"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
    ],
    "jobs": [
        ("assigned_tech_id", "INTEGER REFERENCES users(id)"),
        ("job_type", "VARCHAR(10) DEFAULT 'service'"),
        ("completed_by_id", "INTEGER REFERENCES users(id)"),
        ("started_at", "DATETIME"),
        ("rule_id", "INTEGER REFERENCES maintenance_rules(id)"),
        ("hours", "FLOAT DEFAULT 0.0"),
        ("tax_rate_ppm", "INTEGER"),
        ("labor_override_cents", "INTEGER"),
        ("paid_cash", "BOOLEAN DEFAULT 0"),
    ],
    "job_templates": [("est_hours", "FLOAT DEFAULT 1.0")],
    "job_template_prices": [("hours", "FLOAT DEFAULT 0.0")],
    "visits": [
        ("completed_by_id", "INTEGER REFERENCES users(id)"),
        ("started_at", "DATETIME"),
        ("tax_rate_ppm", "INTEGER"),
        ("labor_override_cents", "INTEGER"),
        ("paid_cash", "BOOLEAN DEFAULT 0"),
    ],
    "chemicals": [
        ("role", "VARCHAR(20) DEFAULT 'other'"),
        ("strength_pct", "FLOAT DEFAULT 100"),
        ("markup_percent", "FLOAT"),
        ("stock_qty", "REAL DEFAULT 0"),
        ("reorder_point", "REAL"),
        ("reorder_qty", "REAL"),
    ],
    "customers": [("tags", "TEXT DEFAULT '[]'"), ("notify_email", "BOOLEAN DEFAULT 0")],
    "equipment": [("warranty_end", "DATE")],
    "photos": [("tag", "VARCHAR(10) DEFAULT ''")],
    "pools": [("kind", "VARCHAR(8) DEFAULT 'pool'"), ("gallons_override", "INTEGER")],
    "size_presets": [("ref_gallons", "INTEGER")],
    "rate_tiers": [("kind", "VARCHAR(8) DEFAULT 'pool'")],
    "invoices": [
        ("subtotal_cents", "INTEGER"),
        ("tax_cents", "INTEGER DEFAULT 0"),
    ],
}


def run(engine) -> None:
    with engine.begin() as conn:
        for table, cols in COLUMNS.items():
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:  # table doesn't exist yet; create_all will make it complete
                continue
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

        # Best-effort backfill of structured address from the legacy free-text
        # `address` for rows that have an address but no city yet. Never
        # overwrites `address`; unparseable rows keep address + blank fields.
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(properties)"))}
        if {"street", "city", "address"} <= cols:
            rows = conn.execute(text(
                "SELECT id, address FROM properties WHERE address != '' AND city = ''")).all()
            for pid, address in rows:
                d = addresses.parse_address(address)
                if d["street"]:
                    conn.execute(text(
                        "UPDATE properties SET street=:s, city=:c, state=:st, zip=:z WHERE id=:id"),
                        {"s": d["street"], "c": d["city"], "st": d["state"], "z": d["zip"], "id": pid})

        # Backfill indexes on existing databases (new DBs get them via create_all).
        # Only create an index once every column it references exists — a
        # partially-migrated legacy table may not have them all yet.
        for name, table, columns in INDEXES:
            present = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            wanted = [c.strip() for c in columns.split(",")]
            if present and all(c in present for c in wanted):
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))

        _dedupe_and_unique_visits(conn)

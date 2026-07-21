"""Additive-only SQLite migrations, run at startup before seeds."""
from sqlalchemy import text

from app import addresses

COLUMNS = {
    "users": [
        ("username", "VARCHAR(40)"),
        ("active", "BOOLEAN DEFAULT 1"),
        ("view_money", "BOOLEAN DEFAULT 0"),
        ("manage_customers", "BOOLEAN DEFAULT 0"),
        ("can_repairs", "BOOLEAN DEFAULT 0"),
        ("view_all_routes", "BOOLEAN DEFAULT 0"),
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
    ],
    "visits": [
        ("completed_by_id", "INTEGER REFERENCES users(id)"),
        ("started_at", "DATETIME"),
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

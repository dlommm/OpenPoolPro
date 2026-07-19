"""Additive-only SQLite migrations, run at startup before seeds."""
from sqlalchemy import text

COLUMNS = {
    "users": [
        ("username", "VARCHAR(40)"),
        ("active", "BOOLEAN DEFAULT 1"),
        ("view_money", "BOOLEAN DEFAULT 0"),
        ("manage_customers", "BOOLEAN DEFAULT 0"),
        ("can_repairs", "BOOLEAN DEFAULT 0"),
        ("view_all_routes", "BOOLEAN DEFAULT 0"),
    ],
    "properties": [("assigned_tech_id", "INTEGER REFERENCES users(id)")],
    "jobs": [
        ("assigned_tech_id", "INTEGER REFERENCES users(id)"),
        ("job_type", "VARCHAR(10) DEFAULT 'service'"),
        ("completed_by_id", "INTEGER REFERENCES users(id)"),
    ],
    "visits": [("completed_by_id", "INTEGER REFERENCES users(id)")],
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

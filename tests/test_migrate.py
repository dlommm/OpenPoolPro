from sqlalchemy import text
from sqlalchemy.orm import Session

from app import migrate, models


def _cols(engine, table):
    with engine.connect() as c:
        return {r[1] for r in c.execute(text(f"PRAGMA table_info({table})"))}


def test_migrate_adds_columns_to_legacy_db(engine):
    # Simulate a Phase 0 database: create schema WITHOUT the new columns
    with engine.connect() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(80), "
                       "role VARCHAR(12), password_hash VARCHAR(200))"))
        c.execute(text("CREATE TABLE properties (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE visits (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE chemicals (id INTEGER PRIMARY KEY, name VARCHAR(80))"))
        c.commit()
    migrate.run(engine)
    migrate.run(engine)  # idempotent
    assert {"username", "active", "view_money", "manage_customers", "can_repairs",
            "view_all_routes", "totp_secret", "totp_enabled", "session_epoch"} <= _cols(engine, "users")
    assert "assigned_tech_id" in _cols(engine, "properties")
    assert {"assigned_tech_id", "job_type", "completed_by_id", "started_at"} <= _cols(engine, "jobs")
    assert {"completed_by_id", "started_at"} <= _cols(engine, "visits")
    assert {"role", "strength_pct"} <= _cols(engine, "chemicals")


def test_migrate_adds_2fa_columns_with_defaults(engine):
    with engine.connect() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(80), "
                       "role VARCHAR(12), password_hash VARCHAR(200))"))
        c.commit()
    migrate.run(engine)
    with engine.connect() as c:
        c.execute(text("INSERT INTO users (id, name, role) VALUES (1, 'A', 'admin')"))
        c.commit()
        row = c.execute(text("SELECT totp_secret, totp_enabled, session_epoch FROM users "
                             "WHERE id = 1")).fetchone()
    assert row[0] is None
    assert row[1] == 0
    assert row[2] == 0


def test_user_can_matrix(engine):
    admin = models.User(name="A", role="admin")
    tech = models.User(name="T", role="tech", view_money=True)
    assert admin.can("view_money") and admin.can("manage_customers")
    assert tech.can("view_money") is True
    assert tech.can("manage_customers") is False
    assert tech.can("nonexistent_flag") is False


def test_rate_changes_table_created_fresh(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    assert _cols(engine, "rate_changes")

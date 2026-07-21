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
        c.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(120))"))
        c.execute(text("CREATE TABLE equipment (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE photos (id INTEGER PRIMARY KEY)"))
        c.commit()
    migrate.run(engine)
    migrate.run(engine)  # idempotent
    assert {"username", "active", "view_money", "manage_customers", "can_repairs",
            "view_all_routes", "totp_secret", "totp_enabled", "session_epoch"} <= _cols(engine, "users")
    assert "assigned_tech_id" in _cols(engine, "properties")
    assert {"assigned_tech_id", "job_type", "completed_by_id", "started_at",
            "rule_id"} <= _cols(engine, "jobs")
    assert {"completed_by_id", "started_at"} <= _cols(engine, "visits")
    assert {"role", "strength_pct", "stock_qty", "reorder_point", "reorder_qty"} <= _cols(engine, "chemicals")
    assert {"tags", "notify_email"} <= _cols(engine, "customers")
    assert "warranty_end" in _cols(engine, "equipment")
    assert "tag" in _cols(engine, "photos")


def test_migrate_new_column_defaults_on_legacy_rows(engine):
    # Legacy rows inserted before migration should backfill sane defaults
    with engine.connect() as c:
        c.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(120))"))
        c.execute(text("CREATE TABLE equipment (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE photos (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY)"))
        c.execute(text("INSERT INTO customers (id, name) VALUES (1, 'Legacy Co')"))
        c.execute(text("INSERT INTO equipment (id) VALUES (1)"))
        c.execute(text("INSERT INTO photos (id) VALUES (1)"))
        c.execute(text("INSERT INTO jobs (id) VALUES (1)"))
        c.commit()
    migrate.run(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT tags FROM customers WHERE id=1")).scalar() == "[]"
        assert c.execute(text("SELECT notify_email FROM customers WHERE id=1")).scalar() == 0
        assert c.execute(text("SELECT warranty_end FROM equipment WHERE id=1")).scalar() is None
        assert c.execute(text("SELECT tag FROM photos WHERE id=1")).scalar() == ""
        assert c.execute(text("SELECT rule_id FROM jobs WHERE id=1")).scalar() is None


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


def test_migrate_preserves_preexisting_row_with_2fa_defaults(engine):
    # A row inserted into the legacy (pre-2FA) schema BEFORE migrate.run()
    # must still read back sane 2FA defaults afterward.
    with engine.connect() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(80), "
                       "role VARCHAR(12), password_hash VARCHAR(200))"))
        c.execute(text("INSERT INTO users (id, name, role) VALUES (1, 'Legacy', 'admin')"))
        c.commit()

    migrate.run(engine)

    with Session(engine) as s:
        u = s.get(models.User, 1)
        assert u is not None
        assert u.totp_secret is None
        assert not u.totp_enabled
        assert u.session_epoch == 0


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

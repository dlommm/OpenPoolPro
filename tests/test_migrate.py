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


def test_migrate_adds_spa_columns(engine):
    from sqlalchemy import text
    from app import migrate
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE pools (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE rate_tiers (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE properties (id INTEGER PRIMARY KEY)"))
    migrate.run(engine)
    with engine.begin() as conn:
        pool_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(pools)"))}
        tier_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(rate_tiers)"))}
        prop_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(properties)"))}
    assert "kind" in pool_cols
    assert "kind" in tier_cols
    assert "spa_rate_override_cents" in prop_cols


def test_migrate_dedupes_duplicate_visits_and_enforces_uniqueness(engine):
    from datetime import date
    from sqlalchemy.exc import IntegrityError

    # A pre-fix DB with two visits on the same (property_id, date): an empty
    # leftover "due" row plus the real completed one.
    with engine.begin() as conn:
        # users must exist because migrate adds visits.completed_by_id REFERENCES
        # users(id); real/create_all DBs always have it.
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, property_id INTEGER, date DATE, "
            "status VARCHAR(8), completed_at DATETIME)"))
        conn.execute(text("CREATE TABLE visit_chemicals (id INTEGER PRIMARY KEY, visit_id INTEGER)"))
        conn.execute(text("INSERT INTO visits (id, property_id, date, status, completed_at) "
                          "VALUES (1, 7, '2026-07-13', 'due', NULL)"))
        conn.execute(text("INSERT INTO visits (id, property_id, date, status, completed_at) "
                          "VALUES (2, 7, '2026-07-13', 'done', '2026-07-13 15:00:00')"))
        conn.execute(text("INSERT INTO visit_chemicals (id, visit_id) VALUES (1, 2)"))

    migrate.run(engine)

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, status FROM visits WHERE property_id=7 AND date='2026-07-13'")).all()
        assert len(rows) == 1 and rows[0][1] == "done"   # kept the completed one
        # its chemical line survived
        assert conn.execute(text("SELECT COUNT(*) FROM visit_chemicals")).scalar() == 1
        # a further duplicate insert is now rejected by the unique index
        try:
            conn.execute(text("INSERT INTO visits (property_id, date, status) "
                              "VALUES (7, '2026-07-13', 'due')"))
            raised = False
        except IntegrityError:
            raised = True
        assert raised


def test_migrate_voids_duplicate_live_invoices_and_enforces_uniqueness(engine):
    from sqlalchemy.exc import IntegrityError

    # A pre-fix DB where the concurrent-generate race double-billed customer 5
    # for July: two live invoices, the second of which the business actually
    # sent and took a payment on. Customer 6 has a legitimate void+regenerate
    # pair that must NOT be touched.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE invoices (id INTEGER PRIMARY KEY, number VARCHAR(20) UNIQUE, "
            "customer_id INTEGER, period_year INTEGER, period_month INTEGER, "
            "total_cents INTEGER, status VARCHAR(8), pdf_filename VARCHAR(200))"))
        conn.execute(text(
            "CREATE TABLE payments (id INTEGER PRIMARY KEY, invoice_id INTEGER, "
            "amount_cents INTEGER)"))
        conn.execute(text("INSERT INTO invoices VALUES "
                          "(1, 'INV-2026-0001', 5, 2026, 7, 5000, 'draft', 'a.pdf'), "
                          "(2, 'INV-2026-0002', 5, 2026, 7, 5000, 'sent', 'b.pdf'), "
                          "(3, 'INV-2026-0003', 6, 2026, 7, 100, 'void', 'c.pdf'), "
                          "(4, 'INV-2026-0004', 6, 2026, 7, 100, 'draft', 'd.pdf')"))
        conn.execute(text("INSERT INTO payments VALUES (1, 2, 5000)"))

    migrate.run(engine)
    migrate.run(engine)  # idempotent

    with engine.begin() as conn:
        status = dict(conn.execute(text("SELECT id, status FROM invoices")).all())
        # Kept the paid/sent duplicate, voided (not deleted) the stray draft;
        # customer 6's pair untouched.
        assert status == {1: "void", 2: "sent", 3: "void", 4: "draft"}
        # The payment stayed attached to the surviving invoice.
        assert conn.execute(text("SELECT invoice_id FROM payments")).scalar() == 2
        # A second live invoice for an already-billed period is now rejected...
        try:
            conn.execute(text(
                "INSERT INTO invoices (number, customer_id, period_year, period_month, "
                "total_cents, status, pdf_filename) "
                "VALUES ('INV-2026-0005', 5, 2026, 7, 1, 'draft', 'e.pdf')"))
            raised = False
        except IntegrityError:
            raised = True
        assert raised
    # ...but void rows stay outside the index, so void-then-regenerate still works.
    with engine.begin() as conn:
        conn.execute(text("UPDATE invoices SET status='void' WHERE id=2"))
        conn.execute(text(
            "INSERT INTO invoices (number, customer_id, period_year, period_month, "
            "total_cents, status, pdf_filename) "
            "VALUES ('INV-2026-0006', 5, 2026, 7, 1, 'draft', 'f.pdf')"))


def test_before_change_fires_once_and_only_when_migrating(engine):
    # A legacy DB with pending column adds: the hook fires exactly once, even
    # though many ALTERs run. A second migrate.run finds nothing to do and
    # must NOT fire — the common no-op boot never pays for a snapshot.
    with engine.connect() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(80), "
                       "role VARCHAR(12), password_hash VARCHAR(200))"))
        c.commit()
    calls = []
    migrate.run(engine, before_change=lambda: calls.append(1))
    assert len(calls) == 1
    migrate.run(engine, before_change=lambda: calls.append(2))
    assert len(calls) == 1


def test_pre_migration_snapshot_captures_the_pre_migration_state(tmp_path, monkeypatch):
    """The point of the whole hook: when a migration is about to DELETE rows
    (the duplicate-visit repair), the snapshot taken by before_change must
    still contain them — it is the pre-image, not a copy of the result."""
    import sqlite3

    from app import backup, config
    from app.db import make_engine

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    engine = make_engine(f"sqlite:///{tmp_path}/app.db")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        c.execute(text(
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, property_id INTEGER, date DATE, "
            "status VARCHAR(8), completed_at DATETIME)"))
        c.execute(text("CREATE TABLE visit_chemicals (id INTEGER PRIMARY KEY, visit_id INTEGER)"))
        c.execute(text("INSERT INTO visits (id, property_id, date, status) "
                       "VALUES (1, 7, '2026-07-13', 'due'), (2, 7, '2026-07-13', 'done')"))

    migrate.run(engine, before_change=lambda: backup.pre_migration_backup(engine))

    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM visits")).scalar() == 1  # repair ran
    snaps = sorted((tmp_path / "backups").glob("pre-migrate-*.db"))
    assert len(snaps) == 1
    with sqlite3.connect(snaps[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 2  # pre-image

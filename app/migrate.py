"""Additive-only SQLite migrations, run at startup before seeds."""
from sqlalchemy import bindparam, text

from app import addresses


def _index_exists(conn, table: str, name: str) -> bool:
    return any(r[1] == name for r in conn.execute(text(f"PRAGMA index_list({table})")))


def _dedupe_and_unique_visits(conn, guard) -> None:
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
            guard()
            conn.execute(text("DELETE FROM visit_chemicals WHERE visit_id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": losers})
            conn.execute(text("DELETE FROM visits WHERE id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": losers})
    if not _index_exists(conn, "visits", "uq_visits_property_date"):
        guard()
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_visits_property_date ON visits (property_id, date)"))

def _encrypt_stored_secrets(conn, guard) -> None:
    """Encrypt credential settings that predate encryption-at-rest.

    Without this, an existing deployment's SMTP password and OAuth refresh
    tokens would sit in the database as plaintext until each was next re-saved
    by hand — and every nightly backup shipped off-box would carry them. Runs
    once: already-encrypted values (enc.v1. prefix) are skipped, so a second
    boot is a no-op.
    """
    from app import secretbox
    from app.models import ENCRYPTED_SETTING_KEYS

    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(settings)"))}
    if not {"key", "value"} <= cols:
        return
    rows = conn.execute(text(
        "SELECT key, value FROM settings WHERE key IN :keys AND value != ''")
        .bindparams(bindparam("keys", expanding=True)),
        {"keys": sorted(ENCRYPTED_SETTING_KEYS)}).all()
    for key, value in rows:
        if secretbox.is_encrypted(value):
            continue
        guard()
        conn.execute(text("UPDATE settings SET value=:v WHERE key=:k"),
                     {"v": secretbox.encrypt(value), "k": key})


def _dedupe_and_unique_invoice_period(conn, guard) -> None:
    """A customer has at most one live (non-void) invoice per billing period.
    generate_month's exists-check and its commit are separated by PDF
    rendering, so two concurrent generates could both pass the check and
    double-bill a customer. Void any duplicates already created by that race —
    keeping the row the business actually used (has payments > was sent >
    oldest id); voiding (not deleting) preserves the number, PDF and any
    attached payments for audit — then add a partial UNIQUE index so the
    invariant is enforced from here on (generate_month catches the
    IntegrityError and counts the loser as skipped)."""
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(invoices)"))}
    if not {"id", "customer_id", "period_year", "period_month", "status"} <= cols:
        return
    # payments always exists alongside invoices after create_all, but a bare
    # migrate.run on a legacy dump may predate it — rank without it then.
    has_payments = bool({r[1] for r in conn.execute(text("PRAGMA table_info(payments)"))})
    paid_expr = ("COALESCE((SELECT SUM(p.amount_cents) FROM payments p "
                 "WHERE p.invoice_id = i.id), 0)" if has_payments else "0")
    groups = conn.execute(text(
        "SELECT customer_id, period_year, period_month FROM invoices "
        "WHERE status != 'void' "
        "GROUP BY customer_id, period_year, period_month HAVING COUNT(*) > 1")).all()
    for customer_id, year, month in groups:
        rows = conn.execute(text(
            f"SELECT i.id, i.status, {paid_expr} "
            "FROM invoices i WHERE i.customer_id=:c AND i.period_year=:y "
            "AND i.period_month=:m AND i.status != 'void'"),
            {"c": customer_id, "y": year, "m": month}).all()

        def rank(r):  # higher = keep
            return (1 if r[2] > 0 else 0, 1 if r[1] == "sent" else 0, -r[0])

        losers = [r[0] for r in sorted(rows, key=rank, reverse=True)[1:]]
        if losers:
            guard()
            conn.execute(text("UPDATE invoices SET status='void' WHERE id IN :ids")
                         .bindparams(bindparam("ids", expanding=True)), {"ids": losers})
    if not _index_exists(conn, "invoices", "uq_invoices_customer_period_live"):
        guard()
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_invoices_customer_period_live "
            "ON invoices (customer_id, period_year, period_month) WHERE status != 'void'"))


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
    # SQLAlchemy does not index a ForeignKey column unless asked, so every one
    # of these was a full table scan. visit_chemicals is the fastest-growing
    # table in the schema (~2 rows per visit) and is scanned by every
    # selectinload of Visit.chemicals — analytics, the QBO rollup, invoicing
    # and the customer timeline all pay it repeatedly.
    ("ix_visit_chemicals_visit_id", "visit_chemicals", "visit_id"),
    ("ix_jobs_property_id", "jobs", "property_id"),
    ("ix_jobs_completed_at", "jobs", "completed_at"),
    ("ix_jobs_scheduled_date", "jobs", "scheduled_date"),
    ("ix_properties_customer_id", "properties", "customer_id"),
    ("ix_service_holds_property_start", "service_holds", "property_id, start_date"),
    ("ix_audit_log_user_id", "audit_log", "user_id"),
    ("ix_invoices_customer_period", "invoices", "customer_id, period_year, period_month"),
    ("ix_comm_log_customer_id", "comm_log", "customer_id"),
    ("ix_photos_taken_at", "photos", "taken_at"),
    ("ix_payments_invoice_id", "payments", "invoice_id"),
    # Every public /sign hit looks a contract up by token hash — without this
    # the hottest unauthenticated query in the app is a full table scan.
    ("ix_contracts_sign_token_hash", "contracts", "sign_token_hash"),
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
        ("approve_billing", "BOOLEAN DEFAULT 0"),
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
    "customers": [("tags", "TEXT DEFAULT '[]'"), ("notify_email", "BOOLEAN DEFAULT 0"),
                  ("qbo_customer_id", "VARCHAR(40) DEFAULT ''"),
                  ("qbo_customer_scope", "VARCHAR(100) DEFAULT ''")],
    "equipment": [("warranty_end", "DATE")],
    "photos": [("tag", "VARCHAR(10) DEFAULT ''")],
    "pools": [("kind", "VARCHAR(8) DEFAULT 'pool'"), ("gallons_override", "INTEGER")],
    "size_presets": [("ref_gallons", "INTEGER")],
    "rate_tiers": [("kind", "VARCHAR(8) DEFAULT 'pool'")],
    "invoices": [
        ("subtotal_cents", "INTEGER"),
        ("tax_cents", "INTEGER DEFAULT 0"),
        ("due_date", "DATE"),
        ("sent_at", "DATETIME"),
    ],
    "contracts": [
        ("sign_token_hash", "VARCHAR(64) DEFAULT ''"),
        ("sign_token_expires_at", "DATETIME"),
        ("signed_name", "VARCHAR(120) DEFAULT ''"),
        ("signed_ip", "VARCHAR(64) DEFAULT ''"),
        ("sign_viewed_at", "DATETIME"),
        ("signed_pdf_hash", "VARCHAR(64) DEFAULT ''"),
    ],
}


def run(engine, before_change=None) -> None:
    """Apply pending migrations.

    `before_change` is called AT MOST ONCE, immediately before the first
    statement that would actually modify the database (ALTER TABLE, UPDATE,
    DELETE, CREATE INDEX) — a boot with nothing to migrate never calls it.
    create_app wires it to backup.pre_migration_backup on real boots: the
    dedupe repairs above DELETE and UPDATE rows, and by upgrade time the
    newest nightly snapshot can be ~23 hours stale, so a data-rewriting
    migration with a bug would otherwise have no same-day pre-image. Any new
    migration step added here must route its writes behind guard() the same
    way, or it silently opts out of that protection.
    """
    fired = False

    def guard():
        nonlocal fired
        if not fired:
            fired = True
            if before_change is not None:
                before_change()

    with engine.begin() as conn:
        for table, cols in COLUMNS.items():
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:  # table doesn't exist yet; create_all will make it complete
                continue
            for name, ddl in cols:
                if name not in existing:
                    guard()
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
                    guard()
                    conn.execute(text(
                        "UPDATE properties SET street=:s, city=:c, state=:st, zip=:z WHERE id=:id"),
                        {"s": d["street"], "c": d["city"], "st": d["state"], "z": d["zip"], "id": pid})

        # Backfill indexes on existing databases (new DBs get them via create_all).
        # Only create an index once every column it references exists — a
        # partially-migrated legacy table may not have them all yet.
        for name, table, columns in INDEXES:
            present = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            wanted = [c.strip() for c in columns.split(",")]
            if (present and all(c in present for c in wanted)
                    and not _index_exists(conn, table, name)):
                guard()
                conn.execute(text(f"CREATE INDEX {name} ON {table} ({columns})"))

        _dedupe_and_unique_visits(conn, guard)
        _dedupe_and_unique_invoice_period(conn, guard)
        _encrypt_stored_secrets(conn, guard)

"""Regression pins for the security audit fixes.

Each test here corresponds to a hole that was reachable before the fix and is
verified closed after it. They are grouped by the property being protected
rather than by router, since several fixes span files.
"""
import io

import pyotp
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
from app import models, security
from app.main import create_app
from app.routers import auth


@pytest.fixture(autouse=True)
def _clean_limiter():
    """auth.limiter is a process-lifetime singleton; reset around each test so
    lock state can't leak between them (mirrors tests/test_account.py)."""
    auth.limiter.reset_all()
    auth._spent_pending.clear()
    yield
    auth.limiter.reset_all()
    auth._spent_pending.clear()


def jpeg_bytes(w=400, h=300):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "blue").save(buf, format="JPEG")
    return buf.getvalue()


def _seed_property(engine, **kw):
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", address="1 St", active=True,
                            weekday=0, frequency="weekly", route_order=1, **kw)
        s.add(p)
        s.commit()
        return c.id, p.id


# --------------------------------------------------------------- session ----

def test_logout_revokes_the_cookie_server_side(client, engine):
    """The session cookie is a stateless 90-day signed token, so clearing the
    browser's copy is not revocation -- logout must bump session_epoch."""
    stolen = client.cookies.get("session")
    assert client.post("/logout", follow_redirects=False).status_code == 303

    replay = TestClient(create_app(engine=engine))
    replay.cookies.set("session", stolen)
    r = replay.get("/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        assert owner.session_epoch == 1


def test_logout_is_audited(client, engine):
    client.post("/logout", follow_redirects=False)
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "auth.logout")).all()
    assert len(rows) == 1


# ------------------------------------------------------------------ 2FA ----

def test_enabling_2fa_requires_the_current_password(client, engine):
    """Confirming enrollment binds an authenticator and evicts every other
    session, so a hijacked session alone must not be able to do it."""
    r = client.post("/account/2fa/enable", follow_redirects=False)
    assert r.status_code == 200                      # re-rendered with an error
    assert "cpp_2fa_enroll" not in r.cookies
    assert "Current password is incorrect" in r.text

    r = client.post("/account/2fa/enable", data={"current_password": "wrong"},
                    follow_redirects=False)
    assert r.status_code == 200 and "cpp_2fa_enroll" not in r.cookies

    r = client.post("/account/2fa/enable", data={"current_password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 303 and "cpp_2fa_enroll" in r.cookies


def _enable_2fa(client) -> str:
    r = client.post("/account/2fa/enable", data={"current_password": "testpass"},
                    follow_redirects=False)
    secret = security.unsign_pending(config.SECRET_KEY, r.cookies["cpp_2fa_enroll"],
                                     max_age=300)["secret"]
    client.post("/account/2fa/confirm", data={"code": pyotp.TOTP(secret).now()},
                follow_redirects=False)
    return secret


def test_pending_2fa_token_is_single_use(client, engine, monkeypatch):
    """The pending cookie skips the password stage, so it must not be
    replayable by anyone who captures it."""
    secret = _enable_2fa(client)

    victim = TestClient(create_app(engine=engine))
    r = victim.post("/login", data={"username": "owner", "password": "testpass"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"
    captured = victim.cookies.get("cpp_2fa")

    totp = pyotp.TOTP(secret)
    r = victim.post("/login/2fa", data={"code": totp.now()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    replay = TestClient(create_app(engine=engine))
    replay.cookies.set("cpp_2fa", captured)
    # Verify against the TOTP directly rather than sleeping for a new step:
    # the token must be rejected before the code is ever considered.
    r = replay.post("/login/2fa", data={"code": totp.now()}, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_pending_2fa_token_dies_with_the_password(client, engine):
    """A password change / 'log out everywhere' must revoke a token already in
    flight, not leave a 5-minute password-free window open."""
    secret = _enable_2fa(client)

    inflight = TestClient(create_app(engine=engine))
    inflight.post("/login", data={"username": "owner", "password": "testpass"},
                  follow_redirects=False)
    captured = inflight.cookies.get("cpp_2fa")

    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        owner.session_epoch += 1
        s.commit()

    attacker = TestClient(create_app(engine=engine))
    attacker.cookies.set("cpp_2fa", captured)
    r = attacker.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()},
                      follow_redirects=False)
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------- photos ----

def test_photo_delete_requires_manage_customers(client, tech_client, engine):
    """Deleting unlinks both files from disk -- a tech must not be able to
    destroy job evidence. Uploading stays open to any signed-in tech."""
    client.post("/photos/pool/1",
                files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
    with Session(engine) as s:
        pid = s.scalars(select(models.Photo)).one().id

    tech = tech_client(name="nobody")
    assert tech.post(f"/photos/{pid}/delete").status_code == 403
    with Session(engine) as s:
        assert s.get(models.Photo, pid) is not None

    assert tech_client(name="mgr", manage_customers=True).post(
        f"/photos/{pid}/delete").status_code == 200


# ----------------------------------------------------------------- money ----

def test_non_money_tech_cannot_set_the_visit_labor_override(client, tech_client, engine):
    """labor_override_cents IS the billed amount; the form hides the field from
    non-money users but the endpoint has to enforce it too."""
    _, pid = _seed_property(engine, rate_override_cents=12000)
    tech = tech_client(name="nobody")
    tech.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id

    tech.post(f"/visit/{vid}/complete",
              data={"status": "done", "labor_override_dollars": "1.00"},
              follow_redirects=False)
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.rate_cents == 12000
        assert v.labor_override_cents is None


def test_money_tech_can_still_set_the_visit_labor_override(client, tech_client, engine):
    _, pid = _seed_property(engine, rate_override_cents=12000)
    tech = tech_client(name="money", view_money=True)
    tech.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id

    tech.post(f"/visit/{vid}/complete",
              data={"status": "done", "labor_override_dollars": "1.00"},
              follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Visit, vid).labor_override_cents == 100


def test_non_money_tech_cannot_smuggle_job_money_fields_on_create(client, tech_client, engine):
    """Rate/Hours price the job; the form hides both from non-money users and
    edit_job refuses their money writes — but create_job stored a hand-crafted
    `hours` anyway (labor stayed 0, yet the hours poisoned income-per-hour
    analytics). Both fields must be inert on create too."""
    _, pid = _seed_property(engine)
    t = tech_client(name="fieldtech2")
    r = t.post("/jobs/new", data={"property_id": str(pid), "title": "Filter clean",
                                  "description": "", "priority": "normal",
                                  "rate_dollars": "425", "hours": "3", "scheduled_date": "",
                                  "job_type": "service", "assigned_tech_id": ""},
               follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job)).one()
        assert j.hours == 0
        assert j.labor_cents == 0


def test_job_tax_rate_is_frozen_at_first_completion(client, engine):
    """Same frozen-history contract visits honour: re-completing to fix a typo
    must not re-price the job at a rate that moved on since."""
    _, pid = _seed_property(engine, state="NJ")

    def set_nj(ppm):
        with Session(engine) as s:
            row = s.get(models.TaxRate, "NJ")
            if row is None:
                s.add(models.TaxRate(state="NJ", rate_ppm=ppm))
            else:
                row.rate_ppm = ppm
            models.set_setting(s, "sales_tax_enabled", "1")
            s.commit()

    set_nj(66250)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "Repair",
                                       "hours": "2", "rate_dollars": "100"},
                    follow_redirects=False)
    jid = int(r.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/jobs/{jid}/complete", data={}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Job, jid).tax_rate_ppm == 66250

    client.post(f"/jobs/{jid}/reopen", follow_redirects=False)
    set_nj(100000)
    client.post(f"/jobs/{jid}/complete", data={}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Job, jid).tax_rate_ppm == 66250


# ------------------------------------------------------- input validation ----

def test_malformed_scheduled_date_does_not_500(client, engine):
    _, pid = _seed_property(engine)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "Bad date",
                                       "scheduled_date": "not-a-date"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job)).one()
        assert j.scheduled_date is None and j.status == "open"


def test_malformed_anchor_date_does_not_500(client, engine):
    cid, _ = _seed_property(engine)
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Second", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "nonsense",
        "rate_override_dollars": ""}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.scalars(select(models.Property).where(
            models.Property.label == "Second")).one()
        assert p.anchor_date is None


def test_negative_rate_override_is_rejected(client, engine):
    cid, pid = _seed_property(engine)
    client.post(f"/properties/{pid}/edit", data={
        "label": "Main", "address": "1 St", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "-50"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Property, pid).rate_override_cents is None


def test_oversized_password_is_a_validation_error_not_a_500(client):
    r = client.post("/account/password",
                    data={"current_password": "testpass",
                          "new_password": "x" * 5000,
                          "new_password_confirm": "x" * 5000},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "characters" in r.text


# ---------------------------------------------------------------- exports ----

def test_analytics_exports_neutralize_spreadsheet_formulas(client, engine):
    """openpyxl turns a leading '=' into a live formula, and Excel/Sheets
    execute '=' '+' '-' '@' cells on open -- free-text columns must be
    prefixed, the way the QBO and customer exports already do it."""
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        c = models.Customer(name='=HYPERLINK("http://evil","x")', active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", active=True, weekday=0,
                            frequency="weekly", route_order=1, rate_override_cents=12000)
        s.add(p)
        s.flush()
        # A completed visit is what puts the customer on the revenue report.
        s.add(models.Visit(property_id=p.id, date=today, status="done",
                           rate_cents=12000))
        s.commit()

    csv_text = client.get(
        f"/analytics/export?tab=customers&start={today}&end={today}").text
    assert "HYPERLINK" in csv_text, f"customer row missing from export: {csv_text!r}"
    assert "'=HYPERLINK" in csv_text
    assert csv_text.count("=HYPERLINK") == csv_text.count("'=HYPERLINK")

    # The .xlsx path shares table_for with the CSV, so pin it at that seam.
    from app import analytics_export
    _, data, _ = analytics_export.table_for(
        "types", [type("Row", (), {"label": "=cmd", "total_cents": 100})()])
    assert data[0][0] == "'=cmd"


def test_customer_csv_export_neutralizes_enum_looking_columns(client, engine):
    """frequency / assigned tech / water_type are not actually constrained at
    the write side, so they need the same treatment as the free-text cells."""
    from app.qbo import csv_safe
    assert csv_safe("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
    assert csv_safe("+1") == "'+1"
    assert csv_safe("@SUM(1)") == "'@SUM(1)"
    assert csv_safe("weekly") == "weekly"

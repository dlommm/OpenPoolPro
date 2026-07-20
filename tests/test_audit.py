from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
from app import models
from app.main import create_app

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    ),
}


def _assert_security_headers(resp):
    for header, expected in SECURITY_HEADERS.items():
        assert resp.headers.get(header) == expected, f"{header}: {resp.headers.get(header)!r}"


def _seed_rows(engine, n, action="test.bulk", user_id=None, start=None):
    """Insert n AuditLog rows with strictly increasing timestamps (newest
    last) so newest-first ordering + pagination is deterministic regardless
    of how fast the test machine runs."""
    base = start or datetime(2026, 1, 1)
    with Session(engine) as s:
        for i in range(n):
            s.add(models.AuditLog(at=base + timedelta(seconds=i), user_id=user_id,
                                  action=action, detail=f"row {i}"))
        s.commit()


# ---------------------------------------------------------------------------
# Security headers -- every response, including anon /login, authed pages,
# 404s, and static assets.
# ---------------------------------------------------------------------------

def test_headers_on_anonymous_login_page(engine, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "testpass")
    c = TestClient(create_app(engine=engine))
    r = c.get("/login")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_headers_on_authed_home(client):
    r = client.get("/")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_headers_on_404(client):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    _assert_security_headers(r)


def test_headers_on_static_asset(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    _assert_security_headers(r)


def test_headers_on_redirect_when_logged_out(engine, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "testpass")
    c = TestClient(create_app(engine=engine))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303
    _assert_security_headers(r)


# ---------------------------------------------------------------------------
# /audit -- admin-only
# ---------------------------------------------------------------------------

def test_audit_admin_only(client, tech_client):
    t = tech_client(name="fieldhand")
    assert t.get("/audit", follow_redirects=False).status_code == 403


def test_audit_admin_can_view(client):
    r = client.get("/audit")
    assert r.status_code == 200


def test_audit_nav_link_admin_only(client, tech_client):
    assert '/audit' in client.get("/").text
    t = tech_client(name="navcheck")
    assert '/audit' not in t.get("/").text


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_audit_action_prefix_filter(client, engine):
    _seed_rows(engine, 1, action="rates.change")
    _seed_rows(engine, 1, action="rates.bulk_raise")
    _seed_rows(engine, 1, action="user.created")

    r = client.get("/audit", params={"action": "rates."})
    assert r.status_code == 200
    assert "rates.change" in r.text
    assert "rates.bulk_raise" in r.text
    assert "user.created" not in r.text


def test_audit_user_filter(client, engine):
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id

    from app.auth_core import hash_password
    with Session(engine) as s:
        tech = models.User(name="Filterme", username="filterme", role="tech", active=True,
                           password_hash=hash_password("tech-pass-1"))
        s.add(tech)
        s.commit()
        s.refresh(tech)
        tech_id = tech.id

    _seed_rows(engine, 1, action="test.owner_row", user_id=owner_id)
    _seed_rows(engine, 1, action="test.tech_row", user_id=tech_id)

    r = client.get("/audit", params={"user_id": str(tech_id)})
    assert r.status_code == 200
    assert "test.tech_row" in r.text
    assert "test.owner_row" not in r.text
    assert "filterme" in r.text  # dropdown includes the user


def test_audit_none_user_shows_dash(client, engine):
    _seed_rows(engine, 1, action="test.system_row", user_id=None)
    r = client.get("/audit")
    assert r.status_code == 200
    assert "—" in r.text  # em dash placeholder for unattributed rows


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_audit_pagination_100_per_page(client, engine):
    _seed_rows(engine, 150, action="test.pagination")

    r1 = client.get("/audit", params={"action": "test.pagination", "page": "1"})
    assert r1.status_code == 200
    assert r1.text.count("row ") >= 100

    r2 = client.get("/audit", params={"action": "test.pagination", "page": "2"})
    assert r2.status_code == 200
    # Newest-first: row 149 (latest) is on page 1, row 49 is the last on page 2.
    assert "row 49" in r2.text
    assert "row 149" not in r2.text


def test_audit_page_tolerant_parse(client, engine):
    _seed_rows(engine, 5, action="test.tolerant")
    r = client.get("/audit", params={"action": "test.tolerant", "page": "abc"})
    assert r.status_code == 200
    assert "row 4" in r.text  # newest row visible => fell back to page 1

    r2 = client.get("/audit", params={"action": "test.tolerant", "page": "-3"})
    assert r2.status_code == 200
    assert "row 4" in r2.text


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

def test_audit_empty_state(client):
    r = client.get("/audit", params={"action": "nonexistent.prefix.zzz"})
    assert r.status_code == 200
    assert "no audit" in r.text.lower() or "no entries" in r.text.lower() or "nothing" in r.text.lower()

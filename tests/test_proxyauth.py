"""Trusted-header SSO (reverse-proxy auto-login)."""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import config, models
from app.main import create_app


def _client(engine, monkeypatch, *, secret="s3cret-proxy-token", user_header="X-OpenPool-User"):
    monkeypatch.setattr(config, "PROXY_AUTH_SECRET", secret)
    monkeypatch.setattr(config, "PROXY_AUTH_HEADER", "X-OpenPool-Auth")
    monkeypatch.setattr(config, "PROXY_AUTH_USER_HEADER", user_header)
    c = TestClient(create_app(engine=engine))
    c.cookies.clear()  # no session — SSO must stand on its own
    return c


def test_valid_header_logs_in_without_password(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    r = c.get("/", headers={"X-OpenPool-Auth": "s3cret-proxy-token"},
              follow_redirects=False)
    assert r.status_code == 200  # served the app, not a redirect to /login


def test_missing_header_still_redirects_to_login(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_secret_is_rejected(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    r = c.get("/", headers={"X-OpenPool-Auth": "not-the-secret"},
              follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_disabled_when_secret_unset(client, engine, monkeypatch):
    c = _client(engine, monkeypatch, secret="")
    r = c.get("/", headers={"X-OpenPool-Auth": "anything"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_sso_mints_a_cookie_so_next_request_needs_no_header(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    c.get("/", headers={"X-OpenPool-Auth": "s3cret-proxy-token"})
    # Cookie established; subsequent request carries no SSO header.
    r = c.get("/settings", follow_redirects=False)
    assert r.status_code == 200


def test_user_header_maps_to_that_staff_user(client, engine, monkeypatch):
    from app.auth_core import hash_password

    with Session(engine) as s:
        s.add(models.User(name="Sam Tech", username="sam", role="tech", active=True,
                          password_hash=hash_password("x")))
        s.commit()
    c = _client(engine, monkeypatch)
    # Sam is a tech, not admin — /settings is admin-only, so a 403 proves the
    # SSO identity resolved to Sam (not the owner).
    r = c.get("/settings", headers={"X-OpenPool-Auth": "s3cret-proxy-token",
                                    "X-OpenPool-User": "sam"}, follow_redirects=False)
    assert r.status_code == 403


def test_unknown_user_header_falls_back_to_owner(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    r = c.get("/settings", headers={"X-OpenPool-Auth": "s3cret-proxy-token",
                                    "X-OpenPool-User": "ghost"}, follow_redirects=False)
    assert r.status_code == 200  # owner is admin


def test_inactive_mapped_user_is_rejected_not_owner(client, engine, monkeypatch):
    from app.auth_core import hash_password

    with Session(engine) as s:
        s.add(models.User(name="Old Tech", username="old", role="tech", active=False,
                          password_hash=hash_password("x")))
        s.commit()
    c = _client(engine, monkeypatch)
    # An inactive named user must NOT silently escalate to the owner account.
    r = c.get("/", headers={"X-OpenPool-Auth": "s3cret-proxy-token",
                            "X-OpenPool-User": "old"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_password_login_still_works_with_sso_enabled(client, engine, monkeypatch):
    c = _client(engine, monkeypatch)
    r = c.post("/login", data={"username": "owner", "password": "testpass"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != "/login"


def test_connection_check_card_shows_ip_and_headers(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Connection check" in r.text
    assert "Public (client) IP" in r.text

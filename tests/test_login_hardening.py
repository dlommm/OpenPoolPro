"""Internet-exposed login hardening: mandatory 2FA + per-IP request throttle."""
from sqlalchemy.orm import Session

from app import config, models, security
from app.auth_core import hash_password

# ---------- mandatory 2FA ----------

def _set_require_2fa(client, engine, on: bool):
    """Mirror the real toggle: persist the setting AND update the app-state
    cache the auth middleware reads."""
    with Session(engine) as s:
        models.set_setting(s, "require_2fa", "1" if on else "0")
        s.commit()
    client.app.state.require_2fa = on


def test_required_2fa_gates_user_without_totp_to_account(client, engine):
    _set_require_2fa(client, engine, True)  # owner (client fixture) has no TOTP
    r = client.get("/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account?force2fa=1"


def test_required_2fa_toggle_route_updates_the_gate(client, engine):
    # End-to-end through the admin toggle, not the helper.
    client.post("/settings/require-2fa", data={"require_2fa": "on"},
                follow_redirects=False)
    r = client.get("/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account?force2fa=1"


def test_required_2fa_still_allows_account_and_logout(client, engine):
    _set_require_2fa(client, engine, True)
    assert client.get("/account", follow_redirects=False).status_code == 200
    # /logout is reachable so a gated user can get out.
    assert client.post("/logout", follow_redirects=False).status_code == 303


def test_not_required_lets_user_without_2fa_through(client, engine):
    _set_require_2fa(client, engine, False)
    assert client.get("/customers", follow_redirects=False).status_code == 200


def test_required_2fa_does_not_gate_a_user_with_totp(client, engine, monkeypatch):
    from fastapi.testclient import TestClient

    from app import security as sec
    from app.main import create_app

    secret = sec.new_totp_secret()
    with Session(engine) as s:
        s.add(models.User(name="Tia", username="tia", role="admin", active=True,
                          password_hash=hash_password("pw"),
                          totp_secret=secret, totp_enabled=True))
        s.commit()
        models.set_setting(s, "require_2fa", "1")
        s.commit()
    c = TestClient(create_app(engine=engine))
    from app.routers import auth as auth_router
    auth_router.limiter.reset_all()
    c.post("/login", data={"username": "tia", "password": "pw"})
    import pyotp
    c.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
    assert c.get("/customers", follow_redirects=False).status_code == 200


def test_connection_check_and_login_security_cards_render(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Login security" in r.text
    assert "Require 2FA for all staff" in r.text


# ---------- request throttle ----------

def test_throttle_allows_under_limit_and_blocks_over():
    t = security.RequestThrottle(limit=3, window=60, clock=lambda: 1000.0)
    assert [t.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert t.allow("1.2.3.4") is False  # 4th over the limit of 3


def test_throttle_window_rolls_over():
    now = [1000.0]
    t = security.RequestThrottle(limit=2, window=60, clock=lambda: now[0])
    assert t.allow("x") and t.allow("x")
    assert t.allow("x") is False
    now[0] += 61  # window elapsed
    assert t.allow("x") is True


def test_throttle_is_per_key():
    t = security.RequestThrottle(limit=1, window=60, clock=lambda: 5.0)
    assert t.allow("a") is True
    assert t.allow("b") is True  # different IP, own budget
    assert t.allow("a") is False


def test_public_ip_flood_gets_429(engine, monkeypatch):
    """A single public client IP over the limit is rejected with 429 — driven
    through the real ASGI middleware stack with a public peer address."""
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(config, "REQUEST_RATE_LIMIT_PER_MIN", 3)
    from tests.conftest import make_owner
    make_owner(engine)  # else every page sends the visitor to first-run setup
    app = create_app(engine=engine)
    # TestClient lets us set the ASGI scope's client address per request.
    # A routable public IP (documentation ranges like 203.0.113.0/24 are NOT
    # is_global, so the fail-open guard would skip them).
    c = TestClient(app, client=("8.8.8.8", 12345))
    codes = [c.get("/login", follow_redirects=False).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_health_is_never_throttled(client, engine, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(config, "REQUEST_RATE_LIMIT_PER_MIN", 1)
    c = TestClient(create_app(engine=engine))
    for _ in range(20):
        assert c.get("/health").status_code == 200


def test_private_client_ip_is_never_throttled(client, engine, monkeypatch):
    """Fail-open safety: if the app only ever sees a private proxy IP
    (FORWARDED_ALLOW_IPS misconfigured), throttling must not engage — else one
    proxy IP standing in for everyone would lock out all users at once."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(config, "REQUEST_RATE_LIMIT_PER_MIN", 1)
    c = TestClient(create_app(engine=engine))  # TestClient peer is not a public IP
    for _ in range(20):
        assert c.get("/login").status_code == 200

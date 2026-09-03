import base64
import json

import itsdangerous
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
from app import models, security
from app.main import create_app
from app.routers import auth


@pytest.fixture(autouse=True)
def _clean_limiter():
    """The router's RateLimiter is a process-lifetime module singleton, so
    lock state from one test (or another test module) could otherwise leak
    into the next. Reset before and after every test in this file."""
    auth.limiter.reset_all()
    yield
    auth.limiter.reset_all()


def make_client(engine, monkeypatch, password="testpass", base_url="http://testserver"):
    monkeypatch.setattr(config, "APP_PASSWORD", password)
    return TestClient(create_app(engine=engine), base_url=base_url)


def _enable_totp(engine, username="owner") -> str:
    secret = security.new_totp_secret()
    with Session(engine) as s:
        u = s.scalars(select(models.User).where(models.User.username == username)).one()
        u.totp_secret = secret
        u.totp_enabled = True
        s.commit()
    return secret


def _user_id(engine, username="owner") -> int:
    with Session(engine) as s:
        return s.scalars(select(models.User).where(models.User.username == username)).one().id


def _audit_count(engine, action: str) -> int:
    with Session(engine) as s:
        return len(s.scalars(select(models.AuditLog).where(models.AuditLog.action == action)).all())


# ---------------------------------------------------------------------------
# Two-step happy path
# ---------------------------------------------------------------------------

def test_two_step_happy_path_totp(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    secret = _enable_totp(engine)

    r = c.post("/login", data={"username": "owner", "password": "testpass"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"
    assert "cpp_2fa" in r.cookies

    # Password verified but 2FA not yet done: still logged out.
    assert c.get("/", follow_redirects=False).status_code == 303

    code = pyotp.TOTP(secret).now()
    r2 = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    assert _audit_count(engine, "auth.login") == 1


def test_password_only_login_unaffected_when_totp_disabled(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.post("/login", data={"username": "owner", "password": "testpass"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# Wrong code / rate limiting at the 2FA stage
# ---------------------------------------------------------------------------

def test_wrong_totp_rejected_and_counts_toward_rate_limit(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    c.post("/login", data={"username": "owner", "password": "testpass"})

    r = c.post("/login/2fa", data={"code": "000000"}, follow_redirects=False)
    assert r.status_code == 200
    assert "Invalid code" in r.text
    assert c.get("/", follow_redirects=False).status_code == 303  # still not logged in
    assert _audit_count(engine, "auth.fail") == 1


def test_locked_key_blocks_correct_totp_code_at_2fa_stage(engine, monkeypatch):
    """Once the shared rate-limit key is locked (5 wrong /login/2fa codes),
    even a CORRECT TOTP code must still be rejected -- the
    ``limiter.locked(...)`` short-circuit at the top of ``login_2fa`` (before
    ``totp_verify`` is ever called) has to win over a valid code."""
    c = make_client(engine, monkeypatch)
    secret = _enable_totp(engine)
    c.post("/login", data={"username": "owner", "password": "testpass"})

    for _ in range(5):
        r = c.post("/login/2fa", data={"code": "000000"}, follow_redirects=False)
        assert r.status_code == 200
    assert _audit_count(engine, "auth.lockout") >= 1

    code = pyotp.TOTP(secret).now()
    r_locked = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r_locked.status_code == 200
    assert "Invalid code" in r_locked.text
    assert c.get("/", follow_redirects=False).status_code == 303  # never authenticated


def test_2fa_failures_count_toward_shared_rate_limit_keys(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    c.post("/login", data={"username": "owner", "password": "testpass"})
    for _ in range(5):
        r = c.post("/login/2fa", data={"code": "000000"}, follow_redirects=False)
        assert r.status_code == 200

    # A brand-new password-stage attempt is now blocked by the same keys,
    # even with the correct password.
    r_locked = c.post("/login", data={"username": "owner", "password": "testpass"},
                       follow_redirects=False)
    assert r_locked.status_code == 200
    assert "Wrong username or password" in r_locked.text


# ---------------------------------------------------------------------------
# Expired pending token
# ---------------------------------------------------------------------------

def test_expired_pending_token_redirects_to_login(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    c.post("/login", data={"username": "owner", "password": "testpass"})

    # -1 guarantees "expired" regardless of same-second signing/verifying.
    monkeypatch.setattr(auth, "PENDING_2FA_MAX_AGE", -1)

    r = c.get("/login/2fa", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    r2 = c.post("/login/2fa", data={"code": "123456"}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == "/login"


def test_2fa_page_without_pending_cookie_redirects_to_login(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.get("/login/2fa", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Pending-cookie purpose isolation
# ---------------------------------------------------------------------------

def test_enroll_token_replayed_as_login_pending_cookie_rejected(engine, monkeypatch):
    """A stolen/leaked cpp_2fa_enroll token (purpose "2fa_enroll") must not
    be usable as the cpp_2fa login-pending cookie -- otherwise an attacker
    who captured an enroll token could skip the password stage entirely and
    land straight on the 2FA code-entry form for a user who never even
    attempted to log in."""
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    uid = _user_id(engine)
    enroll_token = security.sign_pending(
        config.SECRET_KEY, {"purpose": "2fa_enroll", "user_id": uid, "secret": "AAAAAAAAAAAAAAAA"})
    c.cookies.set(auth.PENDING_2FA_COOKIE, enroll_token)

    r = c.get("/login/2fa", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    r2 = c.post("/login/2fa", data={"code": "123456"}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------

def test_backup_code_works_once_then_fails(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    uid = _user_id(engine)
    plain = "abcd1234ef"
    with Session(engine) as s:
        s.add(models.BackupCode(user_id=uid, code_hash=security.hash_code(plain)))
        s.commit()

    c.post("/login", data={"username": "owner", "password": "testpass"})
    r = c.post("/login/2fa", data={"code": plain}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?backup_remaining=0"
    assert c.get("/", follow_redirects=False).status_code == 200
    assert _audit_count(engine, "auth.backup_code_used") == 1

    with Session(engine) as s:
        bc = s.scalars(select(models.BackupCode).where(models.BackupCode.user_id == uid)).one()
        assert bc.used_at is not None

    c.post("/logout")

    # Second use of the same (now-spent) backup code must fail.
    c.post("/login", data={"username": "owner", "password": "testpass"})
    r2 = c.post("/login/2fa", data={"code": plain}, follow_redirects=False)
    assert r2.status_code == 200 and "Invalid code" in r2.text
    assert _audit_count(engine, "auth.backup_code_used") == 1  # unchanged


def test_backup_code_login_redirect_carries_remaining_count(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    uid = _user_id(engine)
    with Session(engine) as s:
        s.add(models.BackupCode(user_id=uid, code_hash=security.hash_code("abcd1234ef")))
        s.add(models.BackupCode(user_id=uid, code_hash=security.hash_code("ffff000011")))
        s.commit()

    c.post("/login", data={"username": "owner", "password": "testpass"})
    r = c.post("/login/2fa", data={"code": "abcd1234ef"}, follow_redirects=False)
    assert r.status_code == 303
    # One of two codes used -> one left.
    assert r.headers["location"] == "/?backup_remaining=1"


def test_totp_login_redirect_has_no_backup_remaining_param(engine, monkeypatch):
    """Only a backup-code login carries the remaining-count param; a plain
    TOTP-code login redirects to a bare "/"."""
    c = make_client(engine, monkeypatch)
    secret = _enable_totp(engine)
    c.post("/login", data={"username": "owner", "password": "testpass"})
    code = pyotp.TOTP(secret).now()
    r = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Identical responses for unknown-user / bad-password / locked
# ---------------------------------------------------------------------------

def test_locked_bad_password_unknown_user_responses_identical(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r_unknown = c.post("/login", data={"username": "ghost", "password": "x"})
    r_badpw = c.post("/login", data={"username": "owner", "password": "wrong"})
    for _ in range(3):
        c.post("/login", data={"username": "owner", "password": "wrong"})
    r_locked = c.post("/login", data={"username": "owner", "password": "testpass"})  # right password!

    assert r_unknown.status_code == r_badpw.status_code == r_locked.status_code == 200
    # The CSP script nonce differs per request by design and carries no
    # account-state signal — strip it, then the bodies must be identical.
    import re
    denonce = lambda t: re.sub(r'nonce="[^"]*"', 'nonce=""', t)  # noqa: E731
    assert denonce(r_unknown.text) == denonce(r_badpw.text) == denonce(r_locked.text)


# ---------------------------------------------------------------------------
# Lockout / unlock via clock advance
# ---------------------------------------------------------------------------

def test_five_fails_locks_then_success_blocked_until_clock_advance(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    fake = {"t": 1000.0}
    monkeypatch.setattr(auth.limiter, "_clock", lambda: fake["t"])

    for _ in range(5):
        c.post("/login", data={"username": "owner", "password": "wrong"})
    assert _audit_count(engine, "auth.fail") == 5
    lockout_count = _audit_count(engine, "auth.lockout")
    assert lockout_count >= 1

    r_blocked = c.post("/login", data={"username": "owner", "password": "testpass"})
    assert "Wrong username or password" in r_blocked.text
    assert c.get("/", follow_redirects=False).status_code == 303  # never got a session

    # Already-locked attempts don't re-fire the lockout audit ("first lock only").
    assert _audit_count(engine, "auth.lockout") == lockout_count

    fake["t"] += 301  # advance past lock_seconds
    r_ok = c.post("/login", data={"username": "owner", "password": "testpass"}, follow_redirects=False)
    assert r_ok.status_code == 303 and r_ok.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Session epoch
# ---------------------------------------------------------------------------

def test_session_cookie_missing_epoch_field_treated_as_epoch_zero(engine, monkeypatch):
    """Pre-existing, already-issued session cookies were signed before the
    "epoch" field existed. Middleware must treat a missing epoch as 0 (the
    default session_epoch for every user) rather than as a mismatch, or
    this change would silently log out every already-logged-in client."""
    c = make_client(engine, monkeypatch)
    uid = _user_id(engine)

    # Hand-build a legacy-shaped session cookie: same signing scheme as
    # Starlette's SessionMiddleware, but the dict has no "epoch" key.
    signer = itsdangerous.TimestampSigner(config.SECRET_KEY)
    data = base64.b64encode(json.dumps({"user_id": uid}).encode("utf-8"))
    legacy_cookie = signer.sign(data).decode("utf-8")
    c.cookies.set("session", legacy_cookie)

    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_epoch_bump_invalidates_existing_client_cookie(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    c.post("/login", data={"username": "owner", "password": "testpass"})
    assert c.get("/", follow_redirects=False).status_code == 200

    with Session(engine) as s:
        u = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        u.session_epoch += 1
        s.commit()

    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Cookie flags
# ---------------------------------------------------------------------------

def test_pending_cookie_flags_default_no_secure(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    _enable_totp(engine)
    r = c.post("/login", data={"username": "owner", "password": "testpass"}, follow_redirects=False)
    pending = next(h for h in r.headers.get_list("set-cookie") if h.startswith("cpp_2fa="))
    assert "httponly" in pending.lower()
    assert "samesite=lax" in pending.lower()
    assert "secure" not in pending.lower()


def test_pending_and_session_cookie_flags_secure_when_configured(engine, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    # Secure cookies are only re-sent by the client over https — use an
    # https base_url so the round trip below actually carries them back,
    # same as it would through a real TLS-terminating proxy in prod.
    c = make_client(engine, monkeypatch, base_url="https://testserver")
    secret = _enable_totp(engine)

    r = c.post("/login", data={"username": "owner", "password": "testpass"}, follow_redirects=False)
    pending = next(h for h in r.headers.get_list("set-cookie") if h.startswith("cpp_2fa="))
    assert "httponly" in pending.lower()
    assert "samesite=lax" in pending.lower()
    assert "secure" in pending.lower()

    code = pyotp.TOTP(secret).now()
    r2 = c.post("/login/2fa", data={"code": code}, follow_redirects=False)
    session_cookie = next(h for h in r2.headers.get_list("set-cookie") if h.startswith("session="))
    assert "httponly" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()
    assert "secure" in session_cookie.lower()

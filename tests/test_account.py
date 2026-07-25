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
    """auth.limiter is a process-lifetime module singleton -- reset around
    every test in this file so lock state can't leak in from (or out to)
    another test module, mirroring tests/test_auth_2fa.py."""
    auth.limiter.reset_all()
    yield
    auth.limiter.reset_all()


def _owner_id(engine) -> int:
    with Session(engine) as s:
        return s.scalars(select(models.User).where(models.User.username == "owner")).one().id


def _audit_rows(engine, action: str) -> list:
    with Session(engine) as s:
        return s.scalars(select(models.AuditLog).where(models.AuditLog.action == action)).all()


def _start_enroll(client, password: str = "testpass") -> str:
    """POST the enable-2FA form and return the freshly-generated secret,
    decoded straight from the signed enroll cookie (rather than scraping
    the rendered page) so tests aren't coupled to markup. Enrollment is a
    step-up action: it requires the current password."""
    r = client.post("/account/2fa/enable", data={"current_password": password},
                    follow_redirects=False)
    assert r.status_code == 303
    token = r.cookies["cpp_2fa_enroll"]
    payload = security.unsign_pending(config.SECRET_KEY, token, max_age=300)
    assert payload is not None and payload["purpose"] == "2fa_enroll"
    return payload["secret"]


def _enable_2fa(client, password: str = "testpass") -> str:
    """Full self-service enroll flow -> returns the confirmed secret."""
    secret = _start_enroll(client, password)
    code = pyotp.TOTP(secret).now()
    r = client.post("/account/2fa/confirm", data={"code": code})
    assert r.status_code == 200 and "backup" in r.text.lower()
    return secret


# ---------------------------------------------------------------------------
# GET /account
# ---------------------------------------------------------------------------

def test_account_page_loads_for_any_authenticated_user(client, tech_client):
    assert client.get("/account").status_code == 200
    t = tech_client(name="fieldhand")
    assert t.get("/account").status_code == 200


def test_account_nav_link_present_for_all_roles(client, tech_client):
    assert '/account' in client.get("/").text
    t = tech_client(name="navcheck")
    assert '/account' in t.get("/").text


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

def test_password_change_happy_path(client, engine):
    owner_id = _owner_id(engine)
    r = client.post("/account/password", data={
        "current_password": "testpass", "new_password": "new-pass-99",
        "new_password_confirm": "new-pass-99"}, follow_redirects=False)
    assert r.status_code == 303 and "banner=password_changed" in r.headers["location"]

    # Actor's own session survives the epoch bump (fresh cookie, same client).
    assert client.get("/account", follow_redirects=False).status_code == 200

    follow = client.get(r.headers["location"])
    assert "Password changed" in follow.text

    assert len(_audit_rows(engine, "auth.password_changed")) == 1
    with Session(engine) as s:
        u = s.get(models.User, owner_id)
        from app.auth_core import verify_password
        assert verify_password("new-pass-99", u.password_hash)


def test_password_change_wrong_current_keeps_page_with_error(client, engine):
    r = client.post("/account/password", data={
        "current_password": "nope-wrong", "new_password": "new-pass-99",
        "new_password_confirm": "new-pass-99"})
    assert r.status_code == 200
    assert "incorrect" in r.text.lower()
    assert len(_audit_rows(engine, "auth.password_changed")) == 0


def test_password_change_too_short_rejected(client, engine):
    r = client.post("/account/password", data={
        "current_password": "testpass", "new_password": "short",
        "new_password_confirm": "short"})
    assert r.status_code == 200 and "8+ characters" in r.text
    assert len(_audit_rows(engine, "auth.password_changed")) == 0


def test_password_change_mismatch_rejected(client, engine):
    r = client.post("/account/password", data={
        "current_password": "testpass", "new_password": "new-pass-99",
        "new_password_confirm": "different-99"})
    assert r.status_code == 200
    assert "match" in r.text.lower()
    assert len(_audit_rows(engine, "auth.password_changed")) == 0


# ---------------------------------------------------------------------------
# 2FA enrollment
# ---------------------------------------------------------------------------

def test_enroll_start_shows_qr_and_manual_secret(client):
    secret = _start_enroll(client)
    html = client.get("/account").text
    assert secret in html
    assert 'data:image/svg+xml;base64,' in html


def test_login_pending_token_replayed_as_enroll_cookie_rejected(client, engine):
    """Reverse direction of the cross-cookie replay fix: a stolen cpp_2fa
    (login-pending) token must not be usable as the cpp_2fa_enroll cookie.
    It carries purpose "login_2fa" (not "2fa_enroll") and no embedded
    secret, so _enroll_payload must reject it rather than surfacing a QR
    code / enroll secret that was never actually issued."""
    owner_id = _owner_id(engine)
    login_token = security.sign_pending(config.SECRET_KEY,
                                        {"user_id": owner_id, "purpose": "login_2fa"})
    client.cookies.set("cpp_2fa_enroll", login_token)
    html = client.get("/account").text
    assert "data:image/svg+xml;base64," not in html


def test_enroll_confirm_wrong_code_not_enabled(client, engine):
    _start_enroll(client)
    r = client.post("/account/2fa/confirm", data={"code": "000000"})
    assert r.status_code == 200
    assert "try again" in r.text.lower()
    with Session(engine) as s:
        u = s.get(models.User, _owner_id(engine))
        assert not u.totp_enabled and u.totp_secret is None
    assert len(_audit_rows(engine, "auth.2fa_enabled")) == 0


def test_enroll_confirm_right_code_enables_and_shows_codes_once(client, engine):
    owner_id = _owner_id(engine)
    secret = _start_enroll(client)
    code = pyotp.TOTP(secret).now()
    r = client.post("/account/2fa/confirm", data={"code": code})
    assert r.status_code == 200

    import re
    codes = re.findall(r'font-data text-sm">([0-9a-f]{10})</div>', r.text)
    assert len(codes) == 10 and len(set(codes)) == 10

    with Session(engine) as s:
        u = s.get(models.User, owner_id)
        assert u.totp_enabled and u.totp_secret == secret
        assert len(s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).all()) == 10

    assert len(_audit_rows(engine, "auth.2fa_enabled")) == 1

    # Actor stays logged in despite the epoch bump.
    assert client.get("/account", follow_redirects=False).status_code == 200

    # Second GET never shows the plaintext codes again (the count display
    # still legitimately says "10 backup codes remaining" -- only the raw
    # code grid must be gone).
    second = client.get("/account").text
    assert not re.findall(r'font-data text-sm">([0-9a-f]{10})</div>', second)
    assert "Your backup codes" not in second


# ---------------------------------------------------------------------------
# Regenerate / disable (both require a valid code)
# ---------------------------------------------------------------------------

def test_regenerate_requires_valid_code(client, engine):
    _enable_2fa(client)
    r = client.post("/account/2fa/regenerate", data={"code": "000000"})
    assert r.status_code == 200 and "try again" in r.text.lower()
    assert len(_audit_rows(engine, "auth.backup_codes_regenerated")) == 0


def test_regenerate_invalidates_old_codes(client, engine):
    owner_id = _owner_id(engine)
    secret = _enable_2fa(client)
    with Session(engine) as s:
        old_hashes = [bc.code_hash for bc in s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).all()]
    assert len(old_hashes) == 10

    code = pyotp.TOTP(secret).now()
    r = client.post("/account/2fa/regenerate", data={"code": code})
    assert r.status_code == 200

    with Session(engine) as s:
        new_rows = s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).all()
        assert len(new_rows) == 10
        new_hashes = {bc.code_hash for bc in new_rows}
    assert new_hashes.isdisjoint(old_hashes)
    assert len(_audit_rows(engine, "auth.backup_codes_regenerated")) == 1


def test_regenerate_accepts_backup_code_instead_of_totp(client, engine):
    owner_id = _owner_id(engine)
    _enable_2fa(client)
    # Swap in one known-plaintext backup code so the confirm step can be
    # driven by a backup code instead of a TOTP code -- mirrors
    # test_disable_accepts_backup_code_instead_of_totp below.
    with Session(engine) as s:
        for bc in s.scalars(select(models.BackupCode).where(
                models.BackupCode.user_id == owner_id)).all():
            s.delete(bc)
        s.add(models.BackupCode(user_id=owner_id, code_hash=security.hash_code("beefcafe01")))
        s.commit()
    r = client.post("/account/2fa/regenerate", data={"code": "beefcafe01"})
    assert r.status_code == 200
    with Session(engine) as s:
        rows = s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).all()
        assert len(rows) == 10
    assert len(_audit_rows(engine, "auth.backup_codes_regenerated")) == 1


def test_disable_requires_valid_code(client, engine):
    _enable_2fa(client)
    r = client.post("/account/2fa/disable", data={"code": "000000"})
    assert r.status_code == 200 and "try again" in r.text.lower()
    with Session(engine) as s:
        assert s.get(models.User, _owner_id(engine)).totp_enabled
    assert len(_audit_rows(engine, "auth.2fa_disabled")) == 0


def test_disable_with_valid_code_clears_2fa_and_audits(client, engine):
    owner_id = _owner_id(engine)
    secret = _enable_2fa(client)
    code = pyotp.TOTP(secret).now()
    r = client.post("/account/2fa/disable", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and "banner=2fa_disabled" in r.headers["location"]

    with Session(engine) as s:
        u = s.get(models.User, owner_id)
        assert not u.totp_enabled and u.totp_secret is None
        assert len(s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).all()) == 0
    assert len(_audit_rows(engine, "auth.2fa_disabled")) == 1

    # Epoch bump on disable still keeps the actor logged in.
    assert client.get("/account", follow_redirects=False).status_code == 200


def test_disable_accepts_backup_code_instead_of_totp(client, engine):
    owner_id = _owner_id(engine)
    _enable_2fa(client)
    # Swap in one known-plaintext backup code (only hashes are ever stored,
    # so a fresh one is seeded directly) to exercise the "confirm via
    # backup code instead of a TOTP code" path.
    with Session(engine) as s:
        for bc in s.scalars(select(models.BackupCode).where(
                models.BackupCode.user_id == owner_id)).all():
            s.delete(bc)
        s.add(models.BackupCode(user_id=owner_id, code_hash=security.hash_code("deadbeef01")))
        s.commit()
    r = client.post("/account/2fa/disable", data={"code": "deadbeef01"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert not s.get(models.User, owner_id).totp_enabled


# ---------------------------------------------------------------------------
# Backup-code count display
# ---------------------------------------------------------------------------

def test_backup_code_count_displayed_and_decrements_after_login_use(client, engine):
    owner_id = _owner_id(engine)
    _enable_2fa(client)
    assert "10 backup codes remaining" in client.get("/account").text

    with Session(engine) as s:
        bc = s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == owner_id)).first()
        bc.used_at = models.utcnow()
        s.commit()

    assert "9 backup codes remaining" in client.get("/account").text


# ---------------------------------------------------------------------------
# Log out everywhere (self-service)
# ---------------------------------------------------------------------------

def test_logout_everywhere_bumps_epoch_keeps_actor_logged_in(client, engine):
    other = TestClient(create_app(engine=engine))
    other.post("/login", data={"username": "owner", "password": "testpass"})
    assert other.get("/", follow_redirects=False).status_code == 200

    r = client.post("/account/logout-everywhere", follow_redirects=False)
    assert r.status_code == 303 and "banner=logged_out_everywhere" in r.headers["location"]

    # Actor (the client that clicked the button) stays logged in.
    assert client.get("/account", follow_redirects=False).status_code == 200
    # The other, previously-valid session is now dead.
    assert other.get("/", follow_redirects=False).status_code == 303

    assert len(_audit_rows(engine, "auth.logout_everywhere")) == 1


# ---------------------------------------------------------------------------
# Admin: reset a user's 2FA / log out their sessions (app/routers/users.py)
# ---------------------------------------------------------------------------

def _make_tech_with_2fa(client, engine, name="fieldtech", password="techpass"):
    from app.auth_core import hash_password
    with Session(engine) as s:
        u = models.User(name=name.title(), username=name, role="tech", active=True,
                        password_hash=hash_password(password))
        s.add(u)
        s.commit()
        uid = u.id
    tech = TestClient(create_app(engine=engine))
    tech.post("/login", data={"username": name, "password": password})
    secret = _enable_2fa(tech, password)
    return uid, tech, secret


def test_admin_reset_2fa_clears_and_audits(client, engine):
    uid, tech, secret = _make_tech_with_2fa(client, engine)
    owner_id = _owner_id(engine)
    assert tech.get("/", follow_redirects=False).status_code == 200

    r = client.post(f"/users/{uid}/2fa/reset", follow_redirects=False)
    assert r.status_code == 303

    with Session(engine) as s:
        u = s.get(models.User, uid)
        assert not u.totp_enabled and u.totp_secret is None
        assert len(s.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == uid)).all()) == 0

    rows = _audit_rows(engine, "auth.2fa_reset")
    assert len(rows) == 1 and rows[0].user_id == owner_id

    # An admin-triggered 2FA reset must bump the target's session epoch too --
    # otherwise the target's still-live cookie stays valid even though their
    # 2FA has just been ripped out from under them.
    assert tech.get("/", follow_redirects=False).status_code == 303


def test_admin_reset_2fa_tech_forbidden(client, engine, tech_client):
    t = tech_client(name="notallowed")
    assert t.post("/users/1/2fa/reset", follow_redirects=False).status_code == 403


def test_admin_reset_own_2fa_stays_logged_in(client, engine):
    """Mirrors the logout-sessions self-target guard: an admin resetting
    their OWN 2FA must get a fresh epoch in their own session so the very
    request that triggered the reset doesn't log them out."""
    owner_id = _owner_id(engine)
    _enable_2fa(client)

    r = client.post(f"/users/{owner_id}/2fa/reset", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200

    with Session(engine) as s:
        u = s.get(models.User, owner_id)
        assert not u.totp_enabled and u.totp_secret is None


def test_admin_logout_sessions_tech_forbidden(client, engine, tech_client):
    t = tech_client(name="notallowed2")
    assert t.post("/users/1/logout-sessions", follow_redirects=False).status_code == 403


def test_admin_logout_their_sessions_invalidates_target(client, engine):
    uid, tech, secret = _make_tech_with_2fa(client, engine)
    assert tech.get("/", follow_redirects=False).status_code == 200

    r = client.post(f"/users/{uid}/logout-sessions", follow_redirects=False)
    assert r.status_code == 303

    assert tech.get("/", follow_redirects=False).status_code == 303  # kicked to /login
    # The admin who clicked the button is unaffected.
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert len(_audit_rows(engine, "auth.logout_everywhere")) == 1


def test_admin_logout_own_sessions_keeps_fresh_cookie(client, engine):
    owner_id = _owner_id(engine)
    r = client.post(f"/users/{owner_id}/logout-sessions", follow_redirects=False)
    assert r.status_code == 303
    # Bumping your OWN epoch from the admin screen must not log you out of
    # the very request that triggered it.
    assert client.get("/account", follow_redirects=False).status_code == 200


def test_admin_reset_password_invalidates_target_and_audits(client, engine):
    """An admin-triggered password reset must bump the target's session
    epoch (mirroring logout-sessions/2fa-reset above) so an attacker
    holding the target's OLD session cookie is kicked out too, and must
    leave an audit trail like every other credential-affecting admin
    action."""
    owner_id = _owner_id(engine)
    uid, tech, secret = _make_tech_with_2fa(client, engine)
    assert tech.get("/", follow_redirects=False).status_code == 200

    r = client.post(f"/users/{uid}/password", data={"password": "new-pass-99"},
                     follow_redirects=False)
    assert r.status_code == 303

    # The target's existing session cookie is rejected after the reset.
    assert tech.get("/", follow_redirects=False).status_code == 303  # kicked to /login
    # The admin who clicked the button is unaffected.
    assert client.get("/account", follow_redirects=False).status_code == 200

    rows = _audit_rows(engine, "auth.password_changed")
    assert len(rows) == 1
    assert rows[0].user_id == owner_id
    assert "admin reset" in rows[0].detail and "fieldtech" in rows[0].detail

import base64
import json
import time

import itsdangerous
import pyotp
from sqlalchemy.orm import Session

from app import models, security


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

def _mid_step_now() -> int:
    """Timestamp aligned to the middle of the current 30s step, to avoid
    boundary flakiness when generating codes at offsets of the step."""
    step = 30
    now = time.time()
    counter = int(now // step)
    return counter * step + step // 2


def test_totp_verify_accepts_prev_current_next_step():
    secret = security.new_totp_secret()
    t0 = _mid_step_now()
    totp = pyotp.TOTP(secret)
    for offset in (-30, 0, 30):
        code = totp.at(t0 + offset)
        assert security.totp_verify(secret, code) is True, f"offset {offset} should verify"


def test_totp_verify_rejects_far_outside_window():
    secret = security.new_totp_secret()
    t0 = _mid_step_now()
    totp = pyotp.TOTP(secret)
    for offset in (-90, 90):
        code = totp.at(t0 + offset)
        assert security.totp_verify(secret, code) is False, f"offset {offset} should not verify"


def test_totp_verify_tolerant_of_bad_input():
    secret = security.new_totp_secret()
    assert security.totp_verify(secret, None) is False
    assert security.totp_verify(secret, "") is False
    assert security.totp_verify(secret, "   ") is False
    assert security.totp_verify(secret, "garbage") is False
    assert security.totp_verify(secret, "12345") is False  # too short
    assert security.totp_verify(secret, "1234567") is False  # too long
    assert security.totp_verify(secret, "12a456") is False  # non-digit
    assert security.totp_verify(None, "123456") is False
    assert security.totp_verify("", "123456") is False
    assert security.totp_verify("not-a-valid-base32-secret!!!", "123456") is False


def test_provisioning_uri_has_issuer_and_username():
    secret = security.new_totp_secret()
    uri = security.provisioning_uri(secret, "sam")
    assert uri.startswith("otpauth://totp/")
    assert "OpenPoolPro" in uri  # issuer is now OpenPoolPro (single word, no URL-encoded space)
    assert "sam" in uri


def test_qr_svg_data_uri_is_base64_svg():
    uri = security.provisioning_uri(security.new_totp_secret(), "sam")
    data_uri = security.qr_svg_data_uri(uri)
    assert data_uri.startswith("data:image/svg+xml;base64,")
    import base64
    payload = data_uri.split(",", 1)[1]
    svg = base64.b64decode(payload).decode("utf-8")
    assert "<svg" in svg


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------

def test_make_backup_codes_returns_10_unique_10char_hex():
    codes = security.make_backup_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert len(c) == 10
        int(c, 16)  # raises if not hex


def test_hash_code_salted_but_verify_code_accepts_both_and_normalizes():
    h1 = security.hash_code("ABCD1234EF")
    h2 = security.hash_code("abcd1234ef")
    # bcrypt salts each hash, so identical input produces different hashes...
    assert h1 != h2
    # ...but both verify against the original code (and its normalized forms).
    assert security.verify_code("ABCD1234EF", h1) is True
    assert security.verify_code("abcd1234ef", h1) is True
    assert security.verify_code(" AbCd1234Ef ", h1) is True
    assert security.verify_code("ABCD1234EF", h2) is True
    # sanity: wrong code fails
    assert security.verify_code("0000000000", h1) is False


def test_verify_code_tolerant_of_bad_input():
    h = security.hash_code("ABCD1234EF")
    assert security.verify_code(None, h) is False
    assert security.verify_code("", h) is False
    assert security.verify_code("   ", h) is False
    assert security.verify_code("ABCD1234EF", None) is False
    assert security.verify_code("ABCD1234EF", "") is False
    assert security.verify_code("ABCD1234EF", "not-a-valid-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# sign_pending / unsign_pending
# ---------------------------------------------------------------------------

def test_unsign_pending_round_trips_payload():
    token = security.sign_pending("supersecret", {"user_id": 7})
    assert security.unsign_pending("supersecret", token) == {"user_id": 7}


def test_unsign_pending_rejects_tampered_token():
    token = security.sign_pending("supersecret", {"user_id": 7})
    # Flip an INTERIOR character (index 1, inside the payload segment — never
    # the "." separators, never the final char). The signature's urlsafe-b64
    # tail carries 2 unused padding bits, so a last-char flip (e.g. "Y" ->
    # "a", identical top 4 bits) can decode to the very same bytes and be
    # legitimately accepted — that made this test flake ~6% of runs.
    ch = token[1]
    flipped = "a" if ch != "a" else "b"
    tampered = token[0] + flipped + token[2:]
    assert tampered != token
    assert security.unsign_pending("supersecret", tampered) is None


def test_unsign_pending_rejects_session_cookie_from_other_direction():
    """Cross-use, direction 1: a cookie signed the way Starlette's
    SessionMiddleware signs the real session cookie (same secret key,
    itsdangerous.TimestampSigner, but no salt and standard b64 encoding)
    must NOT be accepted by unsign_pending -- the distinct
    ``cpp-2fa-pending`` salt has to keep the two cookie namespaces from
    being forgeable into one another."""
    secret = "supersecret"
    signer = itsdangerous.TimestampSigner(secret)  # what SessionMiddleware uses
    data = base64.b64encode(json.dumps({"user_id": 7, "epoch": 0}).encode("utf-8"))
    session_cookie = signer.sign(data).decode("utf-8")
    assert security.unsign_pending(secret, session_cookie) is None


def test_sign_pending_token_rejected_by_session_signer_other_direction():
    """Cross-use, direction 2: a token minted by sign_pending must NOT
    verify against the real session cookie's signer (same secret key, no
    salt) -- a stolen/leaked pending-2FA cookie can't be replayed as a
    real session cookie either."""
    secret = "supersecret"
    pending_token = security.sign_pending(secret, {"user_id": 7})
    session_signer = itsdangerous.TimestampSigner(secret)
    try:
        session_signer.unsign(pending_token.encode("utf-8"), max_age=300)
        raised = False
    except itsdangerous.BadSignature:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

def test_rate_limiter_locks_after_max_fails_and_reset_clears():
    clock = {"t": 1000.0}
    rl = security.RateLimiter(max_fails=5, lock_seconds=300, clock=lambda: clock["t"])
    key = "tester"

    assert rl.locked(key) is False
    for _ in range(4):
        rl.fail(key)
    assert rl.locked(key) is False  # only 4 fails so far

    rl.fail(key)  # 5th fail triggers lock
    assert rl.locked(key) is True

    rl.reset(key)
    assert rl.locked(key) is False


def test_rate_limiter_unlocks_after_lock_seconds_via_injected_clock():
    clock = {"t": 0.0}
    rl = security.RateLimiter(max_fails=5, lock_seconds=300, clock=lambda: clock["t"])
    key = "tester"

    for _ in range(5):
        rl.fail(key)
    assert rl.locked(key) is True

    clock["t"] += 299
    assert rl.locked(key) is True

    clock["t"] += 2  # total elapsed = 301 > 300
    assert rl.locked(key) is False


def test_rate_limiter_keys_are_independent():
    clock = {"t": 0.0}
    rl = security.RateLimiter(max_fails=5, lock_seconds=300, clock=lambda: clock["t"])
    for _ in range(5):
        rl.fail("alice")
    assert rl.locked("alice") is True
    assert rl.locked("bob") is False


def test_rate_limiter_default_clock_is_monotonic():
    rl = security.RateLimiter()
    assert rl.max_fails == 5
    assert rl.lock_seconds == 300
    # default clock should behave sanely (not raise) when used
    assert rl.locked("anyone") is False


def test_rate_limiter_stale_subthreshold_key_resets_after_window_expiry():
    clock = {"t": 0.0}
    rl = security.RateLimiter(max_fails=5, lock_seconds=300, clock=lambda: clock["t"])
    key = "tester"

    for _ in range(3):
        rl.fail(key)
    assert rl._fails[key] == (3, 0.0)
    assert rl.locked(key) is False

    clock["t"] += 301  # window has aged past lock_seconds without locking
    rl.fail(key)
    count, window_start = rl._fails[key]
    assert count == 1  # reset, not accumulated to 4
    assert window_start == clock["t"]
    assert rl.locked(key) is False

    # confirm the reset really happened: it takes a fresh max_fails run to lock
    for _ in range(3):
        rl.fail(key)
    assert rl.locked(key) is False  # only 4 in the new window so far
    rl.fail(key)
    assert rl.locked(key) is True  # 5th in the new window locks


def test_rate_limiter_prunes_expired_stale_keys_past_threshold():
    clock = {"t": 0.0}
    rl = security.RateLimiter(max_fails=5, lock_seconds=300, clock=lambda: clock["t"])

    # Construct >1024 stale, sub-threshold, expired-window entries directly.
    for i in range(1025):
        rl._fails[f"stale-{i}"] = (1, 0.0)
    rl._locked_until["stale-locked"] = 100.0  # also already-expired lock
    assert len(rl._fails) == 1025

    clock["t"] = 400.0  # 400 - 0 > lock_seconds (300): all stale windows expired
    rl.fail("fresh-key")  # crosses the 1024 threshold -> opportunistic prune

    assert len(rl._fails) < 1025
    assert "fresh-key" in rl._fails
    assert all(not k.startswith("stale-") for k in rl._fails)
    assert "stale-locked" not in rl._locked_until


# ---------------------------------------------------------------------------
# Models: User 2FA/session columns, BackupCode, AuditLog, audit()
# ---------------------------------------------------------------------------

def test_user_2fa_columns_defaults(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        u = models.User(name="A", role="admin")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.totp_secret is None
        assert u.totp_enabled is False
        assert u.session_epoch == 0


def test_backup_code_model(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        u = models.User(name="A", role="admin")
        s.add(u)
        s.commit()
        code_hash = security.hash_code("abcd1234ef")
        bc = models.BackupCode(user_id=u.id, code_hash=code_hash)
        s.add(bc)
        s.commit()
        s.refresh(bc)
        assert bc.used_at is None
        assert bc.code_hash == code_hash
        assert security.verify_code("abcd1234ef", bc.code_hash) is True


def test_audit_writes_row_with_utcnow(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        u = models.User(name="A", role="admin")
        s.add(u)
        s.commit()
        before = models.utcnow()
        models.audit(s, u.id, "login_success", detail="via password")
        s.commit()
        after = models.utcnow()
        row = s.query(models.AuditLog).one()
        assert row.user_id == u.id
        assert row.action == "login_success"
        assert row.detail == "via password"
        assert before <= row.at <= after


def test_audit_allows_nullable_user_id(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.audit(s, None, "login_fail", detail="unknown-user")
        s.commit()
        row = s.query(models.AuditLog).one()
        assert row.user_id is None
        assert row.action == "login_fail"

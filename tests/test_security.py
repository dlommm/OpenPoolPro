import time

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
    assert "Coastline%20Pools" in uri or "Coastline+Pools" in uri
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

def test_make_backup_codes_returns_10_unique_8char_hex():
    codes = security.make_backup_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert len(c) == 8
        int(c, 16)  # raises if not hex


def test_hash_code_stable_and_case_space_insensitive():
    h1 = security.hash_code("ABCD1234")
    h2 = security.hash_code(" abcd1234 ")
    h3 = security.hash_code("abcd1234")
    assert h1 == h2 == h3
    assert len(h1) == 64  # sha256 hex digest length
    # sanity: different code -> different hash
    assert security.hash_code("00000000") != h1


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
        bc = models.BackupCode(user_id=u.id, code_hash=security.hash_code("abcd1234"))
        s.add(bc)
        s.commit()
        s.refresh(bc)
        assert bc.used_at is None
        assert bc.code_hash == security.hash_code("abcd1234")


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

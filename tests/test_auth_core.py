from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, security, seeds
from app.auth_core import MAX_PASSWORD_LEN, hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("s3cret")
    assert h != "s3cret" and h.startswith("$2")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("x", None) is False
    assert verify_password("x", "not-a-hash") is False


# Produced by passlib 1.7.4 (CryptContext(schemes=["bcrypt"]), bcrypt 4.0.1),
# which hashed every password and backup code stored before auth_core called
# bcrypt directly. Those rows must keep verifying after the switch.
_PASSLIB_HASH = "$2b$04$uo/GLXyUBcNionKvjGXJKuM62vh0nOqH1zYYP8xGWJWqNb1C.8kgq"
_PASSLIB_LONG_PW = "é" * 50 + "tail"  # 104 UTF-8 bytes: past bcrypt's 72
_PASSLIB_LONG_HASH = "$2b$04$7A3K7jHD5QZYIK9hRatwh.wDEYVpTurgye5UflJCKj0nE3pBQQJYO"


def test_passlib_era_hashes_still_verify():
    assert verify_password("correct horse", _PASSLIB_HASH) is True
    assert verify_password("correct horsE", _PASSLIB_HASH) is False
    assert security.verify_code("Correct Horse ", _PASSLIB_HASH) is True


def test_long_password_truncates_like_passlib_instead_of_raising():
    # passlib silently used the first 72 bytes; bcrypt>=5 raises on longer
    # input. A long password must hash (no 500) and old long hashes verify.
    assert verify_password(_PASSLIB_LONG_PW, _PASSLIB_LONG_HASH) is True
    pw = "x" * MAX_PASSWORD_LEN
    assert verify_password(pw, hash_password(pw)) is True


def _boot(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)


def test_fresh_install_creates_no_owner(engine):
    """No owner from the environment any more: first-run setup makes one."""
    _boot(engine)
    with Session(engine) as s:
        assert s.scalars(select(models.User)).all() == []
        assert models.get_setting(s, "setup_status", "") == ""


def test_upgraded_owner_account_must_finish_profile(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.User(name="Owner", username="owner", role="admin", active=True,
                          password_hash=hash_password("old-env-pass")))
        s.commit()
    seeds.run(engine)
    with Session(engine) as s:
        assert models.get_setting(s, "setup_status", "") == "legacy"
        owner = s.scalars(select(models.User)).one()
        assert verify_password("old-env-pass", owner.password_hash)   # never rewritten


def test_named_owner_counts_as_set_up(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.User(name="Alex Morgan", username="alex.morgan", role="admin", active=True,
                          password_hash=hash_password("x" * 12)))
        s.commit()
    seeds.run(engine)
    with Session(engine) as s:
        assert models.get_setting(s, "setup_status", "") == "done"

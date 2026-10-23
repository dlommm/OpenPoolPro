from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
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


def _prep(engine, monkeypatch, password):
    monkeypatch.setattr(config, "APP_PASSWORD", password)
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)


def test_fresh_install_creates_owner(engine, monkeypatch):
    _prep(engine, monkeypatch, "first-pass")
    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        assert owner.role == "admin" and owner.active
        assert verify_password("first-pass", owner.password_hash)
        total = len(s.scalars(select(models.User)).all())
        assert total == 1  # upgraded in place, not duplicated


def test_legacy_phase0_row_upgraded_in_place(engine, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "boot-pass")
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.User(name="Owner", role="owner"))  # Phase 0 shape: no username/hash
        s.commit()
    seeds.run(engine)
    with Session(engine) as s:
        users = s.scalars(select(models.User)).all()
        assert len(users) == 1  # upgraded in place, not duplicated
        u = users[0]
        assert u.username == "owner" and u.role == "admin" and u.active
        assert verify_password("boot-pass", u.password_hash)


def test_env_change_resyncs_owner_password(engine, monkeypatch):
    _prep(engine, monkeypatch, "first-pass")
    monkeypatch.setattr(config, "APP_PASSWORD", "rotated-pass")
    seeds.run(engine)
    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        assert verify_password("rotated-pass", owner.password_hash)

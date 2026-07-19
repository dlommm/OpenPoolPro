from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
from app import models, seeds
from app.auth_core import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("s3cret")
    assert h != "s3cret" and h.startswith("$2")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("x", None) is False
    assert verify_password("x", "not-a-hash") is False


def _prep(engine, monkeypatch, password):
    monkeypatch.setattr(config, "APP_PASSWORD", password)
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)


def test_owner_upgraded_from_phase0_row(engine, monkeypatch):
    _prep(engine, monkeypatch, "first-pass")
    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        assert owner.role == "admin" and owner.active
        assert verify_password("first-pass", owner.password_hash)
        total = len(s.scalars(select(models.User)).all())
        assert total == 1  # upgraded in place, not duplicated


def test_env_change_resyncs_owner_password(engine, monkeypatch):
    _prep(engine, monkeypatch, "first-pass")
    monkeypatch.setattr(config, "APP_PASSWORD", "rotated-pass")
    seeds.run(engine)
    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        assert verify_password("rotated-pass", owner.password_hash)

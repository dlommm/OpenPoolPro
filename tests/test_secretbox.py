"""Encryption at rest for credential settings."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config, migrate, models, secretbox
from app.db import Base


def _fresh_settings(engine, rows: dict):
    """Bare engine -> real schema, with `rows` seeded as raw (plaintext) values."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM settings"))
        for key, value in rows.items():
            conn.execute(text("INSERT INTO settings (key, value) VALUES (:k, :v)"),
                         {"k": key, "v": value})


# ---------- primitive ----------

def test_roundtrip():
    assert secretbox.decrypt(secretbox.encrypt("hunter2")) == "hunter2"


def test_ciphertext_does_not_contain_the_plaintext():
    blob = secretbox.encrypt("super-secret-refresh-token")
    assert "super-secret-refresh-token" not in blob
    assert blob.startswith(secretbox.PREFIX)


def test_nonce_makes_each_encryption_distinct():
    a, b = secretbox.encrypt("same"), secretbox.encrypt("same")
    assert a != b  # random nonce per encryption
    assert secretbox.decrypt(a) == secretbox.decrypt(b) == "same"


def test_empty_stays_empty():
    assert secretbox.encrypt("") == ""
    assert secretbox.decrypt("") == ""


def test_legacy_plaintext_reads_through_unchanged():
    assert secretbox.decrypt("legacy-plain-value") == "legacy-plain-value"
    assert secretbox.is_encrypted("legacy-plain-value") is False


def test_tampered_ciphertext_reads_as_unset(monkeypatch):
    blob = secretbox.encrypt("token")
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    assert secretbox.decrypt(tampered) == ""  # authentication failure -> unset


def test_wrong_secret_key_reads_as_unset_not_crash(monkeypatch):
    blob = secretbox.encrypt("token")
    monkeypatch.setattr(config, "SECRET_KEY", "a-completely-different-secret-key")
    assert secretbox.decrypt(blob) == ""  # degrades to "not configured"


# ---------- settings integration ----------

def test_secret_settings_are_encrypted_on_disk_but_transparent_to_callers(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "qbo_refresh_token", "rt-live-value")
        models.set_setting(s, "smtp_pass", "smtp-password")
        s.commit()
        # Callers see plaintext...
        assert models.get_setting(s, "qbo_refresh_token", "") == "rt-live-value"
        assert models.get_setting(s, "smtp_pass", "") == "smtp-password"
        # ...but the stored bytes are ciphertext.
        raw = dict(s.execute(text("SELECT key, value FROM settings")).all())
        assert "rt-live-value" not in raw["qbo_refresh_token"]
        assert "smtp-password" not in raw["smtp_pass"]
        assert raw["qbo_refresh_token"].startswith(secretbox.PREFIX)


def test_non_secret_settings_stay_plaintext(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "business_name", "Blue Water Pools")
        s.commit()
        raw = dict(s.execute(text("SELECT key, value FROM settings")).all())
        assert raw["business_name"] == "Blue Water Pools"  # searchable, not a credential


def test_every_declared_secret_key_roundtrips(client, engine):
    with Session(engine) as s:
        for key in models.ENCRYPTED_SETTING_KEYS:
            models.set_setting(s, key, f"value-for-{key}")
        s.commit()
        raw = dict(s.execute(text("SELECT key, value FROM settings")).all())
        for key in models.ENCRYPTED_SETTING_KEYS:
            assert models.get_setting(s, key, "") == f"value-for-{key}"
            assert raw[key].startswith(secretbox.PREFIX)


# ---------- migration of pre-existing plaintext ----------

def test_migration_encrypts_legacy_plaintext_secrets(engine):
    _fresh_settings(engine, {
        "qbo_refresh_token": "legacy-qbo_refresh_token",
        "smtp_pass": "legacy-smtp_pass",
        "geoapify_key": "legacy-geoapify_key",
        "business_name": "Pools Inc",
    })

    migrate.run(engine)

    with Session(engine) as s:
        raw = dict(s.execute(text("SELECT key, value FROM settings")).all())
        for key in ("qbo_refresh_token", "smtp_pass", "geoapify_key"):
            assert raw[key].startswith(secretbox.PREFIX), key
            assert f"legacy-{key}" not in raw[key]
            assert models.get_setting(s, key, "") == f"legacy-{key}"  # value preserved
        assert raw["business_name"] == "Pools Inc"  # untouched


def test_migration_is_idempotent(engine):
    _fresh_settings(engine, {"qbo_refresh_token": "legacy-token"})
    migrate.run(engine)
    with Session(engine) as s:
        once = dict(s.execute(text("SELECT key, value FROM settings")).all())["qbo_refresh_token"]
    migrate.run(engine)  # second boot must not double-encrypt
    with Session(engine) as s:
        twice = dict(s.execute(text("SELECT key, value FROM settings")).all())["qbo_refresh_token"]
        assert twice == once
        assert models.get_setting(s, "qbo_refresh_token", "") == "legacy-token"


def test_migration_fires_pre_change_backup_only_when_it_rewrites(engine):
    calls = []
    _fresh_settings(engine, {"smtp_pass": "plain"})
    migrate.run(engine, before_change=lambda: calls.append(1))
    assert calls, "rewriting a plaintext secret must snapshot the DB first"
    calls.clear()
    migrate.run(engine, before_change=lambda: calls.append(1))
    assert not calls  # nothing left to change

"""Small operational hardening: single-instance lock, /health ops fields,
backup .tmp reclamation, and the PWA maskable icon."""
import json
import os
import pathlib

from sqlalchemy import text

import app.config as config
from app import backup
from app.main import _acquire_single_instance_lock


class _FakeApp:
    class state:
        pass


def test_single_instance_lock_refuses_a_second_process(tmp_path, monkeypatch):
    """Rate limiting, 2FA nonce replay protection, invoice numbering, and the
    scheduler are all single-process by design; a second process on the same
    data dir must fail loudly at boot, not degrade silently."""
    import pytest

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    first, second = _FakeApp(), _FakeApp()
    _acquire_single_instance_lock(first)
    with pytest.raises(RuntimeError, match="single-process"):
        _acquire_single_instance_lock(second)
    # Released with the fd: after the holder closes, the next boot proceeds.
    first.state.instance_lock.close()
    _acquire_single_instance_lock(second)
    second.state.instance_lock.close()


def test_health_reports_backup_age(client, tmp_path):
    """A dead scheduler or stale backups used to be visible only as a
    Settings-page banner; /health now carries the fields (still 200) so an
    uptime monitor can alert on them."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "backup_age_hours" not in body  # no backups yet -> field absent

    bdir = tmp_path / "backups"  # client fixture points DATA_DIR at tmp_path
    bdir.mkdir()
    (bdir / "app-2026-01-01.db").write_bytes(b"x")
    body = client.get("/health").json()
    assert body["backup_age_hours"] >= 0
    assert body["backup_fresh"] is True  # mtime is now


def test_run_backup_reclaims_stale_tmp_sidecars(engine, tmp_path, monkeypatch):
    """A crashed run's .tmp from a PREVIOUS date used to sit in backups/
    forever (only the same-day path was reclaimed)."""
    from app.db import Base
    Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "app-2020-01-01.db.tmp").write_bytes(b"dead")
    (bdir / "assets-2020-01-01.tar.gz.tmp").write_bytes(b"dead")
    with engine.connect() as c:
        c.execute(text("SELECT 1"))
    backup.run_backup(engine)
    leftovers = [f for f in os.listdir(bdir) if f.endswith(".tmp")]
    assert leftovers == []


def test_manifest_declares_scope_and_maskable_icon(client):
    m = json.loads(client.get("/manifest.json").text)
    assert m["scope"] == "/"
    maskable = [i for i in m["icons"] if i.get("purpose") == "maskable"]
    assert maskable and maskable[0]["sizes"] == "512x512"
    assert pathlib.Path("app/static/icons/icon-512-maskable.png").is_file()

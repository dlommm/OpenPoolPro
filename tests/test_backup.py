import os

from sqlalchemy.orm import Session

from app import config, models
from app.backup import run_backup, start_scheduler


def test_backup_creates_snapshot_and_prunes(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from app.db import Base
    Base.metadata.create_all(engine)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i in range(20):  # fake old backups
        (bdir / f"app-2026-06-{i + 1:02d}.db").write_bytes(b"x")
    path = run_backup(engine)
    assert os.path.exists(path)
    remaining = sorted(os.listdir(bdir))
    assert len(remaining) == 14
    assert os.path.basename(path) in remaining


def test_backup_error_recorded(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "missing" / "deeper"))
    from app.db import Base
    Base.metadata.create_all(engine)
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    try:
        run_backup(engine)
    except OSError:
        pass
    with Session(engine) as s:
        assert "disk full" in models.get_setting(s, "last_backup_error", "")


def test_backup_success_clears_prior_error(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "last_backup_error", "old boom")
        s.commit()
    run_backup(engine)
    with Session(engine) as s:
        assert models.get_setting(s, "last_backup_error", "") == ""


def test_start_scheduler_wiring(engine):
    sched = start_scheduler(engine)
    try:
        jobs = sched.get_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.func is run_backup
        assert tuple(job.args) == (engine,)
        trigger = str(job.trigger)
        assert "hour='1'" in trigger and "minute='0'" in trigger
    finally:
        sched.shutdown(wait=False)


# --- off-box sync (BACKUP_SYNC_CMD) ---------------------------------------------

def _sync_setup(engine, monkeypatch, tmp_path, cmd):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "BACKUP_SYNC_CMD", cmd)
    from app.db import Base
    Base.metadata.create_all(engine)
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "p.jpg").write_bytes(b"jpg")


def _all_settings(engine):
    with Session(engine) as s:
        return {r.key: r.value for r in s.query(models.Setting).all()}


def test_offbox_sync_gets_snapshot_paths_and_clears_prior_error(engine, monkeypatch, tmp_path):
    seen = tmp_path / "seen.txt"
    _sync_setup(engine, monkeypatch, tmp_path,
                f'printf "%s\\n%s\\n%s\\n" "$BACKUP_DIR" "$BACKUP_FILE" "$BACKUP_ASSETS_FILE" > {seen}')
    with Session(engine) as s:
        models.set_setting(s, "last_backup_sync_error", "CalledProcessError (exit 1)")
        s.commit()
    path = run_backup(engine)
    bdir, db_file, assets_file = seen.read_text().splitlines()
    assert bdir == str(tmp_path / "backups")
    assert db_file == path
    assert os.path.basename(assets_file).startswith("assets-") and os.path.exists(assets_file)
    assert _all_settings(engine)["last_backup_sync_error"] == ""


def test_offbox_sync_failure_never_persists_the_command_line(engine, monkeypatch, tmp_path):
    # A sync command routinely carries a credential (rclone --sftp-pass=...).
    # Its failure must record only the error type and exit code: the settings
    # table lands in every nightly snapshot, which is exactly what gets shipped.
    secret = "hunter2-offbox-secret"
    _sync_setup(engine, monkeypatch, tmp_path, f"echo --pass={secret} >&2; exit 3")
    path = run_backup(engine)
    settings = _all_settings(engine)
    assert settings["last_backup_sync_error"] == "CalledProcessError (exit 3)"
    assert not any(secret in (v or "") for v in settings.values())
    # The local snapshot still counts as a good backup.
    assert os.path.exists(path)
    assert settings["last_backup_ok"] == "1" and settings["last_backup_error"] == ""


def test_offbox_sync_timeout_records_type_without_command(engine, monkeypatch, tmp_path):
    import subprocess

    secret = "hunter2-offbox-secret"
    _sync_setup(engine, monkeypatch, tmp_path, f"rclone copy --pass={secret} remote:")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    run_backup(engine)
    settings = _all_settings(engine)
    assert settings["last_backup_sync_error"] == "TimeoutExpired"
    assert not any(secret in (v or "") for v in settings.values())


def test_offbox_sync_skipped_when_unconfigured(engine, monkeypatch, tmp_path):
    import subprocess

    _sync_setup(engine, monkeypatch, tmp_path, "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    run_backup(engine)
    assert "last_backup_sync_error" not in _all_settings(engine)

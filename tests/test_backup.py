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

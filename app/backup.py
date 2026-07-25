import os
import subprocess
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config, models


def _sync_offbox(engine, bdir: str, path: str) -> None:
    """Ship the fresh snapshot off the box via the operator-configured command
    (BACKUP_SYNC_CMD). Best-effort: a sync failure is recorded to
    last_backup_sync_error but never fails the backup itself, since the local
    snapshot already succeeded."""
    cmd = config.BACKUP_SYNC_CMD
    if not cmd:
        return
    env = {**os.environ, "BACKUP_DIR": bdir, "BACKUP_FILE": path}
    try:
        subprocess.run(cmd, shell=True, env=env, check=True,
                       capture_output=True, timeout=600)
        err = ""
    except Exception as e:
        # Record the failure TYPE only, never str(e): CalledProcessError and
        # TimeoutExpired both interpolate the full command line, which for a
        # sync command like `rclone --sftp-pass=... ` would persist that
        # credential into the DB and from there into every nightly snapshot.
        rc = getattr(e, "returncode", None)
        err = f"{type(e).__name__}" + (f" (exit {rc})" if rc is not None else "")
    with Session(engine) as s:
        models.set_setting(s, "last_backup_sync_error", err)
        s.commit()


def run_backup(engine) -> str:
    try:
        bdir = os.path.join(config.DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(config.tz()).date().isoformat()
        path = os.path.join(bdir, f"app-{stamp}.db")
        # VACUUM INTO a temp path, then atomically rename into place. A process
        # kill (deploy/restart) mid-vacuum otherwise leaves a truncated but
        # valid-looking `app-<date>.db` that sits in the 14-day retention window
        # and fails on restore. os.replace is atomic on the same filesystem.
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        with engine.connect() as conn:
            conn.execute(text("VACUUM INTO :p"), {"p": tmp})
        os.replace(tmp, path)
        backups = sorted(f for f in os.listdir(bdir)
                         if f.startswith("app-") and f.endswith(".db"))
        for old in backups[:-14]:
            os.remove(os.path.join(bdir, old))
        with Session(engine) as s:
            models.set_setting(s, "last_backup_error", "")
            s.commit()
        _sync_offbox(engine, bdir, path)
        return path
    except Exception as e:
        with Session(engine) as s:
            models.set_setting(s, "last_backup_error", str(e))
            s.commit()
        raise


def start_scheduler(engine):
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone=str(config.tz()))
    sched.add_job(run_backup, "cron", hour=1, minute=0, args=[engine])
    sched.start()
    return sched

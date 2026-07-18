import os
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config, models


def run_backup(engine) -> str:
    try:
        bdir = os.path.join(config.DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(config.tz()).date().isoformat()
        path = os.path.join(bdir, f"app-{stamp}.db")
        if os.path.exists(path):
            os.remove(path)
        with engine.connect() as conn:
            conn.execute(text("VACUUM INTO :p"), {"p": path})
        backups = sorted(f for f in os.listdir(bdir)
                         if f.startswith("app-") and f.endswith(".db"))
        for old in backups[:-14]:
            os.remove(os.path.join(bdir, old))
        with Session(engine) as s:
            models.set_setting(s, "last_backup_error", "")
            s.commit()
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

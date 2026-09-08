"""Read-only operations health and persistent scheduler run outcomes."""
import shutil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import backup, config, mailer, models, msoauth


def record_run(engine, name, status):
    # Observability must never take down the task it observes.
    try:
        with Session(engine) as session:
            row = session.get(models.OperationRun, name)
            if row is None:
                row = models.OperationRun(name=name)
                session.add(row)
            row.status = status
            if status == "running":
                row.started_at = models.utcnow()
                row.finished_at = None
            else:
                row.finished_at = models.utcnow()
            session.commit()
    except Exception:
        pass


def overview(session, app):
    age = backup.newest_backup_age_hours()
    sched = getattr(app.state, "scheduler", None)
    try:
        disk = shutil.disk_usage(config.DATA_DIR)
        free_gb = round(disk.free / 1024**3, 1)
        disk_low = disk.free < max(1024**3, disk.total * .1)
    except OSError:
        free_gb, disk_low = None, True
    mail_counts = dict(session.execute(select(models.EmailOutbox.status, func.count())
        .group_by(models.EmailOutbox.status)).all())
    runs = session.scalars(select(models.OperationRun).order_by(models.OperationRun.name)).all()
    return {"backup_age": round(age, 1) if age is not None else None,
        "backup_bad": age is None or age >= 36 or models.get_setting(session, "last_backup_ok", "") == "0",
        "backup_at": models.get_setting(session, "last_backup_at", "Never recorded"),
        "offbox_configured": bool(config.BACKUP_SYNC_CMD),
        "offbox_failed": bool(models.get_setting(session, "last_backup_sync_error", "")),
        "scheduler_running": bool(sched and sched.running), "scheduler_enabled": config.RUN_SCHEDULER,
        "free_gb": free_gb, "disk_low": disk_low, "mail_counts": mail_counts,
        "mail_configured": mailer.smtp_configured(session), "o365_connected": msoauth.connected(session),
        "qbo_connected": bool(models.get_setting(session, "qbo_refresh_token", "")),
        "qbo_error": bool(models.get_setting(session, "qbo_last_error", "")),
        "maps_configured": bool(models.get_setting(session, "geoapify_key", "")),
        "runs": runs}

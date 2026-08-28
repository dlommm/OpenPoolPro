import os
import subprocess
import tarfile
import time
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config, models

# Everything in DATA_DIR the DB points at but VACUUM INTO cannot capture.
# photos/ and contracts/ (customer-SIGNED agreements) are irreplaceable: without
# them a restore brings back rows whose Photo.filename / Contract.pdf_filename
# reference files that no longer exist. invoices/ is regenerable but cheap to
# carry. tmp/ (CSV staging) is deliberately absent — it is scratch space.
ASSET_DIRS = ("photos", "invoices", "contracts", "branding")

# Generations kept for BOTH the DB snapshot and the asset archive. The two are
# restored as a pair, so pruning them out of step would leave a snapshot whose
# photos are already gone.
KEEP = 14


def _sync_offbox(engine, bdir: str, path: str, assets: str | None = None) -> None:
    """Ship the fresh snapshot off the box via the operator-configured command
    (BACKUP_SYNC_CMD). Best-effort: a sync failure is recorded to
    last_backup_sync_error but never fails the backup itself, since the local
    snapshot already succeeded."""
    cmd = config.BACKUP_SYNC_CMD
    if not cmd:
        return
    # BACKUP_ASSETS_FILE is purely additive: a command written before assets
    # existed (`rclone copy "$BACKUP_FILE" remote:`) keeps working untouched.
    env = {**os.environ, "BACKUP_DIR": bdir, "BACKUP_FILE": path,
           "BACKUP_ASSETS_FILE": assets or ""}
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


def _prune(bdir: str, prefix: str, suffix: str, keep: int = KEEP) -> None:
    """Keep the newest `keep` generations of one snapshot family. The names are
    date-stamped, so lexical order is chronological order."""
    names = sorted(f for f in os.listdir(bdir)
                   if f.startswith(prefix) and f.endswith(suffix))
    for old in names[:-keep]:
        os.remove(os.path.join(bdir, old))


def _tar_filter(ti: tarfile.TarInfo):
    """Drop the `<name>.tmp` sidecars the atomic PDF writers leave behind: they
    are renamed away mid-run, so archiving one risks failing the whole backup on
    a file that vanished between the directory scan and the read — and a partial
    write restores nothing anyway."""
    return None if ti.name.endswith(".tmp") else ti


def archive_assets(bdir: str, stamp: str) -> str | None:
    """Stream DATA_DIR's non-DB payload into `assets-<date>.tar.gz` beside the DB
    snapshot and return its path.

    Returns None when the install has no asset directories at all (a box nobody
    has uploaded to yet): an empty archive would burn a retention slot and imply
    a restore step that does nothing. A *subset* being absent is normal — those
    are skipped.

    tarfile copies each member through a fixed-size buffer, so a 40 GB photo
    directory costs the same memory as an empty one.
    """
    present = [d for d in ASSET_DIRS
               if os.path.isdir(os.path.join(config.DATA_DIR, d))]
    if not present:
        return None
    path = os.path.join(bdir, f"assets-{stamp}.tar.gz")
    # Same tmp + os.replace discipline as the DB snapshot: a process kill
    # mid-write otherwise leaves a truncated .tar.gz that sits in the retention
    # window looking restorable and blows up only when it is finally needed.
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with tarfile.open(tmp, "w:gz") as tar:
        for name in present:
            tar.add(os.path.join(config.DATA_DIR, name), arcname=name,
                    filter=_tar_filter)
    os.replace(tmp, path)
    return path


# Pre-migration snapshots are an escape hatch, not an archive: enough to
# survive one bad upgrade (and a retry or two), few enough to never crowd the
# nightly retention.
PRE_MIGRATE_KEEP = 3


def pre_migration_backup(engine) -> str | None:
    """DB-only snapshot taken by migrate.run immediately before its first real
    change (see run(before_change=...)).

    Exists because the nightly can be ~23 hours stale by upgrade time and
    migrations DELETE/UPDATE rows (the visit and invoice dedupe repairs) — this
    is the same-minute pre-image when a data-rewriting migration goes wrong.
    DB only: migrations never touch photos/contracts, and the asset archive can
    be tens of GB. Own filename family (pre-migrate-*, keep 3, timestamped to
    the second so upgrade retries don't overwrite the one that matters) so it
    never consumes an app-<date>.db retention slot — restore.sh restores it
    like any snapshot by explicit path.

    Called mid-migration-transaction, before that transaction's first write,
    so the second connection's VACUUM INTO sees exactly the pre-migration
    state. Never raises — a full disk must not stop the app from booting —
    but a failure is logged loudly, unlike the settings-table route the
    nightly uses (mid-migration is no time to write settings rows).
    """
    import logging
    log = logging.getLogger("openpoolpro.backup")
    try:
        bdir = os.path.join(config.DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(config.tz()).strftime("%Y-%m-%dT%H%M%S")
        path = os.path.join(bdir, f"pre-migrate-{stamp}.db")
        tmp = path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        with engine.connect() as conn:
            conn.execute(text("VACUUM INTO :p"), {"p": tmp})
        os.replace(tmp, path)
        _prune(bdir, "pre-migrate-", ".db", keep=PRE_MIGRATE_KEEP)
        log.info("pre-migration snapshot: %s", path)
        return path
    except Exception:
        log.exception("pre-migration snapshot FAILED — migration proceeds without one")
        return None


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
        _prune(bdir, "app-", ".db")
        # Assets carry the DB snapshot's date stamp so a restore can pair them
        # by name, and share its retention so the pair never half-expires.
        assets = archive_assets(bdir, stamp)
        _prune(bdir, "assets-", ".tar.gz")
        with Session(engine) as s:
            models.set_setting(s, "last_backup_error", "")
            models.set_setting(s, "last_backup_ok", "1")
            models.set_setting(s, "last_backup_at",
                               datetime.now(config.tz()).isoformat(timespec="seconds"))
            s.commit()
        _sync_offbox(engine, bdir, path, assets)
        return path
    except Exception as e:
        with Session(engine) as s:
            models.set_setting(s, "last_backup_error", str(e))
            models.set_setting(s, "last_backup_ok", "0")
            s.commit()
        raise


def backup_if_stale(engine, max_age_hours: float = 24.0) -> str | None:
    """Take a catch-up snapshot on boot when the newest one is stale or absent.

    The scheduler is in-process and non-persistent: a box that is down at 01:00
    (night deploy, Unraid reboot, host maintenance) silently skips that night
    and nothing ever notices, so the "14 daily generations" the operator
    believes they have can be 14 copies of one week-old day.

    Never raises — run_backup already records the failure to last_backup_error /
    last_backup_ok, and a full disk must not stop the app from starting.
    Returns the snapshot path when one was taken, else None.
    """
    bdir = os.path.join(config.DATA_DIR, "backups")
    try:
        # mtime, not the last_backup_at setting: after a restore that setting
        # comes back from inside the snapshot and describes a run whose output
        # is not necessarily on this disk.
        newest = max((os.path.getmtime(os.path.join(bdir, f))
                      for f in os.listdir(bdir)
                      if f.startswith("app-") and f.endswith(".db")), default=None)
    except OSError:  # no backups dir yet: first boot, or a restored/empty data dir
        newest = None
    if newest is not None and (time.time() - newest) < max_age_hours * 3600:
        return None
    try:
        return run_backup(engine)
    except Exception:
        return None


def start_scheduler(engine):
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone=str(config.tz()))
    sched.add_job(run_backup, "cron", hour=1, minute=0, args=[engine])
    sched.start()
    return sched

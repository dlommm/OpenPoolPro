"""End-to-end tests for scripts/restore.sh — the disaster-recovery path.

That script will be run exactly once, under stress, by a panicked operator, so
a regression in it is discovered at the most expensive possible moment. These
tests execute the real script against a scratch data dir and pin its safety
rails: the WAL-sidecar removal (the whole reason it isn't `cp`), the
integrity gate on the snapshot, the paired asset restore, and the
nothing-is-ever-deleted move-aside.

`--container ''` disables the is-the-app-running docker check (no container in
CI), and `-y` skips the interactive confirmation, exactly as the header
documents for scripted use.
"""
import pathlib
import sqlite3
import subprocess
import tarfile

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "restore.sh"


def _mk_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT v FROM t")]
    finally:
        con.close()


def _run(args, cwd):
    return subprocess.run(["bash", str(SCRIPT), "-y", "--container", "", *args],
                          capture_output=True, text=True, cwd=cwd, timeout=60)


def _seed_data_dir(tmp_path):
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    _mk_db(data / "app.db", ["live"])
    # Stale WAL sidecars from the previous database: if these survive the
    # restore, SQLite replays their frames over the restored file on next
    # open — the silent corruption the script exists to prevent.
    (data / "app.db-wal").write_bytes(b"stale wal frames")
    (data / "app.db-shm").write_bytes(b"stale shm")
    _mk_db(data / "backups" / "app-2026-07-01.db", ["snapshot"])
    return data


def test_restore_swaps_db_clears_sidecars_and_unpacks_assets(tmp_path):
    data = _seed_data_dir(tmp_path)
    stage = tmp_path / "stage" / "photos"
    stage.mkdir(parents=True)
    (stage / "p.jpg").write_bytes(b"jpeg-bytes")
    with tarfile.open(data / "backups" / "assets-2026-07-01.tar.gz", "w:gz") as tar:
        tar.add(stage, arcname="photos")

    r = _run(["--data-dir", str(data), "2026-07-01"], cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr

    assert _rows(data / "app.db") == ["snapshot"]
    # Sidecars must not exist under their live names (moved aside, not left).
    assert not (data / "app.db-wal").exists()
    assert not (data / "app.db-shm").exists()
    # The paired assets archive was unpacked.
    assert (data / "photos" / "p.jpg").read_bytes() == b"jpeg-bytes"
    # Nothing was deleted: the previous DB is still on disk, moved aside.
    aside = list(data.glob("app.db.pre-restore-*"))
    assert aside and _rows(aside[0]) == ["live"]


def test_restore_refuses_a_corrupt_snapshot_and_leaves_live_db_untouched(tmp_path):
    data = _seed_data_dir(tmp_path)
    # A truncated/garbage snapshot must fail the integrity gate BEFORE anything
    # is moved — restoring it blind would trade a live DB for a broken one.
    (data / "backups" / "app-2026-07-02.db").write_bytes(b"not a sqlite file")

    r = _run(["--data-dir", str(data), "2026-07-02"], cwd=tmp_path)
    assert r.returncode != 0
    assert "integrity" in (r.stdout + r.stderr).lower()
    # Sidecar check FIRST: _rows() below opens the live DB itself, and SQLite
    # discards an invalid WAL sidecar on close — checking after would blame
    # the script for this test's own connection.
    assert (data / "app.db-wal").exists()  # nothing was touched
    assert _rows(data / "app.db") == ["live"]


def test_restore_defaults_to_newest_snapshot(tmp_path):
    data = _seed_data_dir(tmp_path)
    _mk_db(data / "backups" / "app-2026-07-03.db", ["newer snapshot"])

    r = _run(["--data-dir", str(data)], cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(data / "app.db") == ["newer snapshot"]

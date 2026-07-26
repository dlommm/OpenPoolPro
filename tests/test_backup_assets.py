import os
import tarfile
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app import config, models
from app.backup import backup_if_stale, run_backup


def _seed(tmp_path, monkeypatch, *, photos=True, invoices=True,
          contracts=True, branding=True):
    """Point DATA_DIR at tmp_path and lay out the requested slice of the asset
    tree the app writes at runtime, with the same nesting (photos/<type>/<id>,
    contracts/<id>) so the archive is exercised on real-shaped paths."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    if photos:
        d = tmp_path / "photos" / "visit" / "7"
        d.mkdir(parents=True)
        (d / "abc.jpg").write_bytes(b"photo-bytes")
        (d / "abc_t.jpg").write_bytes(b"thumb-bytes")
    if invoices:
        (tmp_path / "invoices").mkdir()
        (tmp_path / "invoices" / "invoice-12.pdf").write_bytes(b"%PDF-inv")
    if contracts:
        d = tmp_path / "contracts" / "3"
        d.mkdir(parents=True)
        (d / "signed.pdf").write_bytes(b"%PDF-signed")
    if branding:
        (tmp_path / "branding").mkdir()
        (tmp_path / "branding" / "logo.png").write_bytes(b"\x89PNG")


def _stamp():
    return datetime.now(config.tz()).date().isoformat()


def _members(tmp_path):
    with tarfile.open(tmp_path / "backups" / f"assets-{_stamp()}.tar.gz") as tar:
        return set(tar.getnames())


def test_asset_archive_captures_photos_invoices_and_signed_contracts(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    run_backup(engine)
    names = _members(tmp_path)
    assert "photos/visit/7/abc.jpg" in names
    assert "invoices/invoice-12.pdf" in names
    assert "contracts/3/signed.pdf" in names
    assert "branding/logo.png" in names


def test_asset_archive_content_round_trips(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    run_backup(engine)
    with tarfile.open(tmp_path / "backups" / f"assets-{_stamp()}.tar.gz") as tar:
        assert tar.extractfile("contracts/3/signed.pdf").read() == b"%PDF-signed"


def test_asset_archive_skips_missing_subdirectories(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch, invoices=False, contracts=False, branding=False)
    run_backup(engine)  # must not raise on the three absent directories
    names = _members(tmp_path)
    assert "photos/visit/7/abc.jpg" in names
    assert not any(n.startswith(("invoices", "contracts", "branding")) for n in names)


def test_no_asset_archive_when_data_dir_has_no_assets(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch, photos=False, invoices=False,
          contracts=False, branding=False)
    run_backup(engine)
    assert not [f for f in os.listdir(tmp_path / "backups") if f.startswith("assets-")]


def test_asset_archive_leaves_no_tmp_file(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    run_backup(engine)
    assert not [f for f in os.listdir(tmp_path / "backups") if f.endswith(".tmp")]


def test_asset_archive_excludes_atomic_write_sidecars(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    (tmp_path / "contracts" / "3" / "contract.pdf.tmp").write_bytes(b"half")
    run_backup(engine)
    assert "contracts/3/contract.pdf.tmp" not in _members(tmp_path)


def test_asset_retention_prunes_in_step_with_db_snapshots(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i in range(20):
        (bdir / f"app-2026-06-{i + 1:02d}.db").write_bytes(b"x")
        (bdir / f"assets-2026-06-{i + 1:02d}.tar.gz").write_bytes(b"x")
    run_backup(engine)
    remaining = os.listdir(bdir)
    assert len([f for f in remaining if f.startswith("app-")]) == 14
    assert len([f for f in remaining if f.startswith("assets-")]) == 14
    assert f"app-{_stamp()}.db" in remaining
    assert f"assets-{_stamp()}.tar.gz" in remaining


def test_backup_records_health_settings_on_success(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    run_backup(engine)
    with Session(engine) as s:
        assert models.get_setting(s, "last_backup_ok", "") == "1"
        assert models.get_setting(s, "last_backup_at", "").startswith(_stamp())


def test_backup_records_health_settings_on_failure(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    try:
        run_backup(engine)
    except OSError:
        pass
    with Session(engine) as s:
        assert models.get_setting(s, "last_backup_ok", "") == "0"


def test_backup_if_stale_runs_when_no_snapshot_exists(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    path = backup_if_stale(engine)
    assert path is not None and os.path.exists(path)
    assert os.path.exists(tmp_path / "backups" / f"assets-{_stamp()}.tar.gz")


def test_backup_if_stale_skips_when_a_fresh_snapshot_exists(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    fresh = bdir / f"app-{_stamp()}.db"
    fresh.write_bytes(b"not-a-real-db")
    assert backup_if_stale(engine) is None
    # Untouched: a real run would have replaced today's file with a snapshot.
    assert fresh.read_bytes() == b"not-a-real-db"
    assert not [f for f in os.listdir(bdir) if f.startswith("assets-")]


def test_backup_if_stale_runs_when_newest_snapshot_is_a_day_old(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    _seed(tmp_path, monkeypatch)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    stale = bdir / "app-2026-06-01.db"
    stale.write_bytes(b"x")
    old = time.time() - 30 * 3600
    os.utime(stale, (old, old))
    assert backup_if_stale(engine) is not None


def test_backup_if_stale_never_raises_on_failure(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert backup_if_stale(engine) is None  # boot must survive a broken data dir
    with Session(engine) as s:
        assert "disk full" in models.get_setting(s, "last_backup_error", "")

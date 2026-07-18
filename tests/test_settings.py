from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def test_update_chemical_cost(client, engine):
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical)).first()
        cid = chem.id
    client.post("/settings/chemicals", data={
        "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
        "cost_dollars": ["4.75"], f"active_{cid}": "on"})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).cost_per_unit_cents == 475


def test_add_and_edit_tier(client, engine):
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier).order_by(models.RateTier.id)).all()
        ids = [str(t.id) for t in tiers]
    client.post("/settings/tiers", data={
        "tier_id": ids + [""],
        "max_gallons": ["15000", "25000", "", "40000"],
        "rate_dollars": ["48", "58", "75", "65"],
        "label": ["S", "M", "XL", "L"]})
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier)).all()
        assert len(tiers) == 4
        by_label = {t.label: t for t in tiers}
        assert by_label["S"].rate_cents == 4800
        assert by_label["L"].max_gallons == 40000


def test_markup_setting(client, engine):
    client.post("/settings/general", data={"chem_markup_percent": "25",
                                           "business_name": "Coastline Pools"})
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "25"


def test_stale_and_garbage_rows_skipped(client, engine):
    client.post("/settings/tiers", data={"tier_id": ["9999", "abc"],
                                         "max_gallons": ["", ""],
                                         "rate_dollars": ["50", "60"],
                                         "label": ["Stale", "Garbage"]})
    with Session(engine) as s:
        labels = {t.label for t in s.scalars(select(models.RateTier)).all()}
        assert "Stale" not in labels and "Garbage" not in labels
    client.post("/settings/chemicals", data={
        "chem_id": ["9999", "abc"], "name": ["X", "Y"], "unit": ["gal", "gal"],
        "cost_dollars": ["1", "2"]})
    client.post("/settings/tasks", data={
        "task_id": ["9999", "abc"], "label": ["X", "Y"]})


def test_tier_delete_via_blank_rate(client, engine):
    with Session(engine) as s:
        tid = s.scalars(select(models.RateTier)).first().id
        before = len(s.scalars(select(models.RateTier)).all())
    client.post("/settings/tiers", data={"tier_id": [str(tid)], "max_gallons": [""],
                                         "rate_dollars": [""], "label": [""]})
    with Session(engine) as s:
        assert len(s.scalars(select(models.RateTier)).all()) == before - 1


def test_chemical_deactivate_via_blank_name(client, engine):
    with Session(engine) as s:
        cid = s.scalars(select(models.Chemical)).first().id
    client.post("/settings/chemicals", data={"chem_id": [str(cid)], "name": [""],
                                             "unit": ["gal"], "cost_dollars": ["0"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).active is False


def test_backup_download_route(client, engine):
    import os
    import app.config as config
    bdir = os.path.join(config.DATA_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "app-2026-07-01.db"), "wb") as f:
        f.write(b"data")
    assert client.get("/settings/backups/app-2026-07-01.db").status_code == 200
    assert client.get("/settings/backups/missing.db").status_code == 404
    assert client.get("/settings/backups/..%2fsecret").status_code in (400, 404)

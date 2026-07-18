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

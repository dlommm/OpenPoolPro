from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, seeds


def _enable_nj(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        models.set_setting(s, "sales_tax_enabled", "1")
        p = s.get(models.Property, 1)
        p.state = "NJ"
        s.commit()


def _seed(client):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "", "phone": "",
                                        "email": "", "notes": ""})
    client.post("/customers/1/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    client.post("/properties/1/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})


def test_visit_complete_freezes_tax_rate(client, engine):
    _seed(client)
    _enable_nj(engine)
    with Session(engine) as s:
        v = models.Visit(property_id=1, date=date(2026, 7, 13), status="due")
        s.add(v); s.commit(); vid = v.id
    client.post(f"/visit/{vid}/complete", data={"started_hhmm": "", "ended_hhmm": ""})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.status == "done"
        assert v.tax_rate_ppm == 66250            # frozen from NJ


def test_visit_complete_stores_labor_override(client, engine):
    _seed(client)
    _enable_nj(engine)
    with Session(engine) as s:
        v = models.Visit(property_id=1, date=date(2026, 7, 13), status="due")
        s.add(v); s.commit(); vid = v.id
    client.post(f"/visit/{vid}/complete",
                data={"started_hhmm": "", "ended_hhmm": "", "labor_override_dollars": "90"})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.labor_override_cents == 9000


def test_job_complete_freezes_tax_rate(client, engine):
    _seed(client)
    _enable_nj(engine)
    client.post("/jobs/new", data={"property_id": "1", "title": "J", "description": "",
                                   "priority": "normal", "rate_dollars": "180", "hours": "1",
                                   "scheduled_date": "", "job_type": "service",
                                   "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    client.post(f"/jobs/{jid}/complete", data={"labor_override_dollars": "270"})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.status == "done"
        assert j.tax_rate_ppm == 66250
        assert j.labor_override_cents == 27000

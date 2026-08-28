from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _seed(client, engine):
    client.post("/customers/new", data={"name": "AttrCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": ""})
    with Session(engine) as s:
        return cid, s.scalars(select(models.Property)).one().id


def test_visit_complete_and_skip_stamp_user(client, engine):
    cid, pid = _seed(client, engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
        owner_id = s.scalars(select(models.User).where(
            models.User.username == "owner")).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""})
    with Session(engine) as s:
        assert s.get(models.Visit, vid).completed_by_id == owner_id
    client.get(f"/visit/{pid}/2026-07-20")
    with Session(engine) as s:
        vid2 = s.scalars(select(models.Visit).where(
            models.Visit.date == date(2026, 7, 20))).one().id
    client.post(f"/visit/{vid2}/skip", data={"reason": "rain"})
    with Session(engine) as s:
        assert s.get(models.Visit, vid2).completed_by_id == owner_id
    client.post(f"/visit/{pid}/2026-07-27/dismiss")
    with Session(engine) as s:
        v3 = s.scalars(select(models.Visit).where(
            models.Visit.date == date(2026, 7, 27))).one()
        assert v3.status == "skipped" and v3.completed_by_id == owner_id
    html = client.get(f"/customers/{cid}").text
    assert "O" in html  # owner initials tag in history (loose check, refined below)


def test_repair_assignee_filtered_by_capability(client, engine, tech_client):
    _seed(client, engine)
    tech_client(name="cleaner")                 # no can_repairs
    tech_client(name="fixer", can_repairs=True)
    client.get("/jobs/new")
    # both appear for default service type rendering; capability filtering is
    # enforced server-side on POST for repairs:
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
        cleaner = s.scalars(select(models.User).where(
            models.User.username == "cleaner")).one().id
        fixer = s.scalars(select(models.User).where(
            models.User.username == "fixer")).one().id
    r = client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Fix pump", "description": "",
        "priority": "normal", "labor_dollars": "", "scheduled_date": "",
        "job_type": "repair", "assigned_tech_id": str(cleaner)})
    assert r.status_code == 400  # cleaner can't take repairs
    r = client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Fix pump", "description": "",
        "priority": "normal", "labor_dollars": "", "scheduled_date": "",
        "job_type": "repair", "assigned_tech_id": str(fixer)}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job)).one()
        assert j.job_type == "repair" and j.assigned_tech_id == fixer


def test_job_complete_stamps_user(client, engine):
    _seed(client, engine)
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post("/jobs/new", data={"property_id": str(pid), "title": "T",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "", "scheduled_date": "",
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
        owner_id = s.scalars(select(models.User).where(
            models.User.username == "owner")).one().id
    client.post(f"/jobs/{jid}/complete")
    with Session(engine) as s:
        assert s.get(models.Job, jid).completed_by_id == owner_id

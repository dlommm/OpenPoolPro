from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def prop(engine):
    with Session(engine) as s:
        c = models.Customer(name="JobCo")
        p = models.Property(customer=c, label="Home")
        s.add(p)
        s.commit()
        return p.id


def test_job_lifecycle(client, engine):
    pid = prop(engine)
    r = client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Filter clean", "description": "DE grids",
        "priority": "high", "labor_dollars": "125", "scheduled_date": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job)).one()
        assert j.status == "open" and j.labor_cents == 12500
    assert "Filter clean" in client.get("/jobs").text
    today = date.today()
    client.post(f"/jobs/{j.id}/schedule", data={"scheduled_date": str(today)})
    with Session(engine) as s:
        assert s.get(models.Job, j.id).status == "scheduled"
    assert "Filter clean" in client.get("/").text  # appears on Today
    client.post(f"/jobs/{j.id}/complete")
    with Session(engine) as s:
        jj = s.get(models.Job, j.id)
        assert jj.status == "done" and jj.completed_at is not None
    assert "Filter clean" not in client.get("/jobs?status=open").text
    assert "Filter clean" in client.get("/jobs?status=done").text


def test_edit_never_undones_and_reopen(client, engine):
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "T", "description": "",
                                   "priority": "normal", "labor_dollars": "50", "scheduled_date": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    client.post(f"/jobs/{jid}/complete")
    client.post(f"/jobs/{jid}/edit", data={"property_id": str(pid), "title": "T2", "description": "",
                                           "priority": "normal", "labor_dollars": "60", "scheduled_date": ""})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.status == "done" and j.title == "T2" and j.labor_cents == 6000
    client.post(f"/jobs/{jid}/reopen")
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.status == "open" and j.completed_at is None


def test_high_priority_sorts_first(client, engine):
    pid = prop(engine)
    for pr, title in [("normal", "NormalJob"), ("high", "HighJob")]:
        client.post("/jobs/new", data={"property_id": str(pid), "title": title, "description": "",
                                       "priority": pr, "labor_dollars": "", "scheduled_date": ""})
    html = client.get("/jobs").text
    assert html.index("HighJob") < html.index("NormalJob")


def test_garbage_labor_dollars_defaults_zero(client, engine):
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "G", "description": "",
                                   "priority": "normal", "labor_dollars": "abc", "scheduled_date": ""})
    with Session(engine) as s:
        assert s.scalars(select(models.Job).where(
            models.Job.title == "G")).one().labor_cents == 0

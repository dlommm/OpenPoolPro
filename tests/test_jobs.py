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

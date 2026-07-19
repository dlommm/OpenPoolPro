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


def test_job_start_and_times(client, engine):
    from datetime import date
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "Timed",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "", "scheduled_date": str(date.today()),
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job).where(models.Job.title == "Timed")).one().id
    client.post(f"/jobs/{jid}/start")
    with Session(engine) as s:
        assert s.get(models.Job, jid).started_at is not None
    client.post(f"/jobs/{jid}/complete")
    client.post(f"/jobs/{jid}/times", data={"started_hhmm": "10:00", "ended_hhmm": "11:05"})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.duration_minutes == 65
    client.post(f"/jobs/{jid}/times", data={"started_hhmm": "xx", "ended_hhmm": ""})
    with Session(engine) as s:
        assert s.get(models.Job, jid).duration_minutes == 65  # tolerant
    client.post(f"/jobs/{jid}/reopen")
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.started_at is not None and j.completed_at is None  # start kept


def test_job_times_noop_keeps_completion_date(client, engine):
    from datetime import date, timedelta
    from app.timefmt import local_date, to_local_hhmm
    pid = prop(engine)
    last_week = (date.today() - timedelta(days=7)).isoformat()
    client.post("/jobs/new", data={"property_id": str(pid), "title": "Anchored",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "", "scheduled_date": last_week,
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job).where(
            models.Job.title == "Anchored")).one().id
    client.post(f"/jobs/{jid}/start")
    client.post(f"/jobs/{jid}/complete")
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        before_end, started = j.completed_at, j.started_at
    client.post(f"/jobs/{jid}/times", data={
        "started_hhmm": to_local_hhmm(started), "ended_hhmm": to_local_hhmm(before_end)})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert local_date(j.completed_at) == local_date(before_end)  # NOT last week
        assert local_date(j.started_at) == local_date(started)


def test_job_times_evening_utc_rollover(client, engine):
    from datetime import datetime
    from app.timefmt import local_date
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "Evening",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "", "scheduled_date": "",
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Evening")).one()
        jid = j.id
        # 2026-07-20 01:30 UTC == 2026-07-19 21:30 ET
        j.started_at = datetime(2026, 7, 20, 1, 0)
        j.completed_at = datetime(2026, 7, 20, 1, 30)
        j.status = "done"
        s.commit()
    client.post(f"/jobs/{jid}/times", data={"started_hhmm": "", "ended_hhmm": "21:45"})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.completed_at == datetime(2026, 7, 20, 1, 45)  # anchored to Jul 19 ET


def test_job_detail_shows_start_button_then_duration(client, engine):
    from datetime import date
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "BtnJob",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "", "scheduled_date": str(date.today()),
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job).where(models.Job.title == "BtnJob")).one().id
    assert "Start job" in client.get(f"/jobs/{jid}").text
    client.post(f"/jobs/{jid}/start")
    client.post(f"/jobs/{jid}/complete")
    client.post(f"/jobs/{jid}/times", data={"started_hhmm": "10:00", "ended_hhmm": "11:05"})
    html = client.get(f"/jobs/{jid}").text
    assert "1 h 05" in html and "Start job" not in html

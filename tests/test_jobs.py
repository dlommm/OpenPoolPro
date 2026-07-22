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
        "priority": "high", "rate_dollars": "50", "hours": "2.5", "scheduled_date": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job)).one()
        assert j.status == "open" and j.labor_cents == 12500 and j.hours == 2.5
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
                                   "priority": "normal", "rate_dollars": "25", "hours": "2",
                                   "scheduled_date": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    client.post(f"/jobs/{jid}/complete")
    client.post(f"/jobs/{jid}/edit", data={"property_id": str(pid), "title": "T2", "description": "",
                                           "priority": "normal", "rate_dollars": "30", "hours": "2",
                                           "scheduled_date": ""})
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
                                       "priority": pr, "rate_dollars": "", "hours": "",
                                       "scheduled_date": ""})
    html = client.get("/jobs").text
    assert html.index("HighJob") < html.index("NormalJob")


def test_garbage_rate_dollars_defaults_zero(client, engine):
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "G", "description": "",
                                   "priority": "normal", "rate_dollars": "abc", "hours": "3",
                                   "scheduled_date": ""})
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "G")).one()
        assert j.labor_cents == 0 and j.hours == 3.0


def test_job_start_and_times(client, engine):
    from datetime import date
    pid = prop(engine)
    client.post("/jobs/new", data={"property_id": str(pid), "title": "Timed",
                                   "description": "", "priority": "normal",
                                   "rate_dollars": "", "scheduled_date": str(date.today()),
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
                                   "rate_dollars": "", "scheduled_date": last_week,
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
                                   "rate_dollars": "", "scheduled_date": "",
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
                                   "rate_dollars": "", "scheduled_date": str(date.today()),
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job).where(models.Job.title == "BtnJob")).one().id
    assert "Start job" in client.get(f"/jobs/{jid}").text
    client.post(f"/jobs/{jid}/start")
    client.post(f"/jobs/{jid}/complete")
    client.post(f"/jobs/{jid}/times", data={"started_hhmm": "10:00", "ended_hhmm": "11:05"})
    html = client.get(f"/jobs/{jid}").text
    assert "1 h 05" in html and "Start job" not in html


def test_create_job_from_template_seeds_tasks(client, engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Eve")
        p = models.Property(customer=c)
        s.add(p)
        t = models.JobTemplate(title="Filter clean", job_type="service", labor_cents=0)
        t.tasks = [models.JobTemplateTask(label="Backwash", position=0),
                   models.JobTemplateTask(label="Rinse", position=1)]
        s.add(t); s.commit()
        pid, tid = p.id, t.id
    r = client.post("/jobs/new", data={
        "property_id": pid, "title": "Filter clean", "job_type": "service",
        "template_id": str(tid)}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        job = s.scalars(select(models.Job).where(models.Job.title == "Filter clean")).one()
        labels = [x.label for x in sorted(job.tasks, key=lambda z: z.position)]
        assert labels == ["Backwash", "Rinse"]
        # No rate/hours posted -> default rate ($180/hr) x the template's flat est_hours (1.0)
        assert job.hours == 1.0 and job.labor_cents == 18000
        jt = job.tasks[0]
        assert jt.done is False
    r2 = client.post(f"/jobs/{job.id}/tasks/{jt.id}/toggle", follow_redirects=False)
    assert r2.status_code == 303
    with Session(engine) as s:
        assert s.get(models.JobTask, jt.id).done is True


def test_create_job_uses_default_rate_setting_with_explicit_hours(client, engine):
    pid = prop(engine)
    with Session(engine) as s:
        models.set_setting(s, "labor_rate_cents", "20000")  # $200/hr
        s.commit()
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "NoRate",
                                       "description": "", "priority": "normal",
                                       "rate_dollars": "", "hours": "3", "scheduled_date": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "NoRate")).one()
        assert j.hours == 3.0 and j.labor_cents == 60000  # $200/hr x 3h


def test_create_job_rate_override(client, engine):
    pid = prop(engine)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "Overridden",
                                       "description": "", "priority": "normal",
                                       "rate_dollars": "75", "hours": "2", "scheduled_date": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Overridden")).one()
        assert j.hours == 2.0 and j.labor_cents == 15000  # $75/hr x 2h


def _template_with_hours_matrix(engine):
    with Session(engine) as s:
        t = models.JobTemplate(title="Green clean", job_type="service", labor_cents=0)
        t.prices = [models.JobTemplatePrice(label="Small", max_gallons=15000, hours=1.5)]
        s.add(t); s.commit()
        return t.id


def _prop_with_pool(engine, gallons, name):
    with Session(engine) as s:
        c = models.Customer(name=name)
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=gallons, kind="pool"))
        s.commit()
        return p.id


def test_create_job_template_size_matched_hours_priced(client, engine):
    pid = _prop_with_pool(engine, 12000, "MatchCo")
    tid = _template_with_hours_matrix(engine)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "Matched",
                                       "job_type": "service", "template_id": str(tid),
                                       "rate_dollars": "100"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Matched")).one()
        assert j.hours == 1.5 and j.labor_cents == 15000  # $100/hr x 1.5h


def test_create_job_template_unmatched_blocks_unless_hours_given(client, engine):
    pid = _prop_with_pool(engine, 40000, "TooBigCo")
    tid = _template_with_hours_matrix(engine)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "TooBig",
                                       "job_type": "service", "template_id": str(tid)},
                    follow_redirects=False)
    assert r.status_code == 400  # 40k matches no size row, no hours entered -> blocked
    r2 = client.post("/jobs/new", data={"property_id": str(pid), "title": "TooBig2",
                                        "job_type": "service", "template_id": str(tid),
                                        "rate_dollars": "100", "hours": "4"},
                     follow_redirects=False)
    assert r2.status_code == 303  # entering hours lets it through
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "TooBig2")).one()
        assert j.hours == 4.0 and j.labor_cents == 40000

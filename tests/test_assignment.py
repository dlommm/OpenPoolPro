from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.scheduling import suggest_tech


def _tech(engine, name):
    from app.auth_core import hash_password
    with Session(engine) as s:
        u = models.User(name=name.title(), username=name, role="tech", active=True,
                        password_hash=hash_password("techpass-1"))
        s.add(u)
        s.commit()
        return u.id


def _prop(engine, name, weekday, tech_id=None):
    with Session(engine) as s:
        c = models.Customer(name=name)
        p = models.Property(customer=c, frequency="weekly", weekday=weekday,
                            anchor_date=date(2026, 1, 5), assigned_tech_id=tech_id)
        s.add(p)
        s.commit()
        return p.id


def test_suggest_tech_majority(client, engine):
    sam = _tech(engine, "sam")
    lee = _tech(engine, "lee")
    _prop(engine, "A", 0, sam)
    _prop(engine, "B", 0, sam)
    _prop(engine, "C", 0, lee)
    with Session(engine) as s:
        assert suggest_tech(s, 0) == sam
        assert suggest_tech(s, 3) is None


def test_tech_sees_own_and_unassigned_only(client, engine, tech_client):
    sam = _tech(engine, "sam")
    today = date.today()
    wd = today.weekday()
    _prop(engine, "Mine", wd, sam)
    _prop(engine, "Unassigned", wd, None)
    with Session(engine) as s:
        other = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    _prop(engine, "SomeoneElses", wd, other)
    t = tech_client(name="sam2")  # separate login user
    # sam2 has no assignments: sees only Unassigned
    html = t.get("/").text
    assert "Unassigned" in html and "SomeoneElses" not in html and "Mine" not in html
    # admin sees everything
    admin_html = client.get("/").text
    for n in ["Mine", "Unassigned", "SomeoneElses"]:
        assert n in admin_html


def test_admin_tech_filter_param(client, engine):
    sam = _tech(engine, "sam")
    wd = date.today().weekday()
    _prop(engine, "SamStop", wd, sam)
    _prop(engine, "FreeStop", wd, None)
    html = client.get(f"/?tech={sam}").text
    assert "SamStop" in html and "FreeStop" not in html


def test_property_form_smart_default(client, engine):
    sam = _tech(engine, "sam")
    _prop(engine, "A", 2, sam)
    _prop(engine, "B", 2, sam)
    client.post("/customers/new", data={"name": "NewGuy", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(
            models.Customer.name == "NewGuy")).one().id
    html = client.get(f"/customers/{cid}/properties/new").text
    assert 'name="assigned_tech_id"' in html
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "2", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": str(sam)})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).join(models.Customer).where(
            models.Customer.name == "NewGuy")).one()
        assert p.assigned_tech_id == sam


def test_disabled_tech_stops_surface_to_others(client, engine, tech_client):
    wd = date.today().weekday()
    sam = _tech(engine, "sam")
    _prop(engine, "OrphanStop", wd, sam)
    t = tech_client(name="other")
    assert "OrphanStop" not in t.get("/").text
    with Session(engine) as s:
        s.get(models.User, sam).active = False
        s.commit()
    assert "OrphanStop" in t.get("/").text
    assert "disabled tech" in client.get("/").text


def test_missed_section_filtered_by_visibility(client, engine, tech_client):
    past = date.today() - timedelta(days=3)
    sam = _tech(engine, "sam3")
    with Session(engine) as s:
        c = models.Customer(name="MissedMine")
        p = models.Property(customer=c, frequency="weekly", weekday=past.weekday(),
                            anchor_date=past - timedelta(days=7), assigned_tech_id=sam)
        s.add(p)
        s.commit()
    t = tech_client(name="other3")
    assert "MissedMine" not in t.get("/").text
    assert "MissedMine" in client.get("/").text


def test_week_visibility_and_filter(client, engine, tech_client):
    wd = date.today().weekday()
    sam = _tech(engine, "sam4")
    _prop(engine, "WeekMine", wd, sam)
    _prop(engine, "WeekFree", wd, None)
    t = tech_client(name="other4")
    html = t.get("/week").text
    assert "WeekFree" in html and "WeekMine" not in html
    admin = client.get(f"/week?tech={sam}").text
    assert "WeekMine" in admin and "WeekFree" not in admin


def test_job_visibility_scoped(client, engine, tech_client):
    wd = date.today().weekday()
    sam = _tech(engine, "sam5")
    pid = _prop(engine, "JobProp", wd, None)
    with Session(engine) as s:
        s.add(models.Job(property_id=pid, title="SamOnlyJob", status="scheduled",
                         scheduled_date=date.today(), assigned_tech_id=sam))
        s.add(models.Job(property_id=pid, title="FreeJob", status="scheduled",
                         scheduled_date=date.today()))
        s.commit()
    t = tech_client(name="other5")
    html = t.get("/").text
    assert "FreeJob" in html and "SamOnlyJob" not in html

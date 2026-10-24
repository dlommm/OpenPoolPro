"""Small gaps found while building V2: property assignees must be able to
hold a route, and deactivating a customer records when."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

PROP = {"label": "Home", "street": "1 Palm Ave", "city": "Cocoa", "state": "FL", "zip": "32922",
        "gate_code": "", "dog_note": "", "notes": "", "frequency": "weekly", "weekday": "0",
        "anchor_date": "", "rate_override_dollars": "", "spa_rate_override_dollars": ""}


def _customer(client, engine):
    client.post("/customers/new", data={"name": "Assign Co", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        return s.scalars(select(models.Customer)).one().id


def _user(engine, **kw):
    with Session(engine) as s:
        u = models.User(name=kw.pop("name"), username=kw.pop("username"), **kw)
        s.add(u)
        s.commit()
        return u.id


def test_property_form_rejects_people_who_cannot_hold_a_route(client, engine):
    cid = _customer(client, engine)
    gone = _user(engine, name="Gone Tech", username="gone", role="tech", active=False)
    tech = _user(engine, name="Real Tech", username="real", role="tech", active=True)
    client.post(f"/customers/{cid}/properties/new", data={**PROP, "assigned_tech_id": str(gone)})
    client.post(f"/customers/{cid}/properties/new", data={**PROP, "assigned_tech_id": "99999"})
    client.post(f"/customers/{cid}/properties/new", data={**PROP, "assigned_tech_id": str(tech)})
    with Session(engine) as s:
        got = [p.assigned_tech_id for p in s.scalars(select(models.Property).order_by(models.Property.id))]
    assert got == [None, None, tech]


def test_editing_keeps_a_now_inactive_assignee_rather_than_dropping_it(client, engine):
    cid = _customer(client, engine)
    tech = _user(engine, name="Later Gone", username="later", role="tech", active=True)
    client.post(f"/customers/{cid}/properties/new", data={**PROP, "assigned_tech_id": str(tech)})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
        s.get(models.User, tech).active = False
        s.commit()
    client.post(f"/properties/{pid}/edit", data={**PROP, "label": "Renamed", "assigned_tech_id": str(tech)})
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        assert p.assigned_tech_id == tech


def test_deactivation_is_dated_and_used_for_lost_customers(client, engine):
    from app import analytics_overview
    cid = _customer(client, engine)
    client.post(f"/customers/{cid}/deactivate")
    with Session(engine) as s:
        c = s.get(models.Customer, cid)
        assert not c.active and c.deactivated_at is not None
        today = date.today()
        mv = analytics_overview.customer_movement(s, [(today.year, today.month)], today)
        assert [m.name for m in mv.lost] == ["Assign Co"]
    client.post(f"/customers/{cid}/reactivate")
    with Session(engine) as s:
        assert s.get(models.Customer, cid).deactivated_at is None


def test_customer_pages_follow_the_palette_switch(client, engine):
    from fastapi.testclient import TestClient
    with Session(engine) as s:
        models.set_setting(s, "palette", "coral")
        s.commit()
    from app import portal
    cid = _customer(client, engine)
    with Session(engine) as s:
        token = portal.issue(s, cid, 30)
        s.commit()
    anon = TestClient(client.app)
    assert "<style>:root" in anon.get(f"/portal/{token}").text
    with Session(engine) as s:
        models.set_setting(s, "email_brand", "0")
        s.commit()
    assert "<style>:root" not in anon.get(f"/portal/{token}").text
    assert "<style>:root" in client.get("/customers").text   # staff pages keep it

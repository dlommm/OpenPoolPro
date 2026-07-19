from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def test_create_and_search_customer(client, engine):
    r = client.post("/customers/new", data={
        "name": "Smith Family", "qb_display_name": "", "phone": "555-1111",
        "email": "s@x.com", "notes": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "Smith Family" in client.get("/customers").text
    assert "Smith Family" in client.get("/customers?q=smi").text
    assert "Smith Family" not in client.get("/customers?q=zzz").text
    with Session(engine) as s:
        c = s.scalars(select(models.Customer)).one()
        assert c.qb_display_name == "Smith Family"  # defaults to name when blank


def test_add_property_with_schedule_and_override(client, engine):
    client.post("/customers/new", data={"name": "Jones", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Beach house", "address": "1 Ocean Dr", "gate_code": "1234",
        "dog_warning": "on", "dog_note": "Big dog", "notes": "",
        "frequency": "biweekly", "weekday": "2", "anchor_date": "2026-07-08",
        "rate_override_dollars": "62.50"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.scalars(select(models.Property)).one()
        assert p.frequency == "biweekly" and p.weekday == 2
        assert p.anchor_date == date(2026, 7, 8)
        assert p.rate_override_cents == 6250
    detail = client.get(f"/customers/{cid}").text
    assert "Beach house" in detail and "1234" in detail


def test_deactivate_customer_hides_from_list(client, engine):
    client.post("/customers/new", data={"name": "Old Guy", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/deactivate")
    assert "Old Guy" not in client.get("/customers").text


def test_deactivate_buttons_present_in_edit_forms(client, engine):
    # I1 pin: the deactivate action must be reachable from the edit forms.
    client.post("/customers/new", data={"name": "Btn Guy", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    customer_html = client.get(f"/customers/{cid}/edit").text
    assert f"/customers/{cid}/deactivate" in customer_html

    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "1 Bay St", "gate_code": "", "dog_note": "",
        "notes": "", "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    property_html = client.get(f"/properties/{pid}/edit").text
    assert f"/properties/{pid}/deactivate" in property_html


def test_deactivated_customer_stop_removed_from_schedule(client, engine):
    # I1 pin: deactivating a customer must pull their still-scheduled weekly
    # stop out of stops_for_date (and therefore out of Missed) even though
    # the underlying property row is untouched.
    from datetime import date, timedelta

    from app.scheduling import missed_stops, stops_for_date

    today = date(2026, 7, 15)  # Wednesday
    with Session(engine) as s:
        c = models.Customer(name="Dropped Guy")
        p = models.Property(customer=c, frequency="weekly", weekday=0,
                            anchor_date=today - timedelta(days=28))
        s.add(p)
        s.commit()
        cid, pid = c.id, p.id
        monday = today - timedelta(days=2)
        assert any(s2.property.id == pid for s2 in stops_for_date(s, monday))
        assert any(m.property.id == pid for m in missed_stops(s, today))

    client.post(f"/customers/{cid}/deactivate")

    with Session(engine) as s:
        assert not any(s2.property.id == pid for s2 in stops_for_date(s, monday))
        assert not any(m.property.id == pid for m in missed_stops(s, today))


def test_missing_records_return_404(client):
    assert client.get("/customers/999").status_code == 404
    assert client.get("/customers/999/edit").status_code == 404
    assert client.get("/properties/999/edit").status_code == 404
    assert client.post("/customers/999/deactivate").status_code == 404
    assert client.post("/properties/999/deactivate").status_code == 404
    assert client.post("/customers/999/edit", data={"name": "X", "qb_display_name": "",
                                                    "phone": "", "email": "", "notes": ""}).status_code == 404
    assert client.get("/customers/999/properties/new").status_code == 404
    assert client.post("/properties/999/edit", data={
        "label": "H", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""}).status_code == 404


def test_history_shows_duration(client, engine):
    from datetime import datetime as dt
    client.post("/customers/new", data={"name": "DurCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(
            models.Customer.name == "DurCo")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": ""})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).join(models.Customer).where(
            models.Customer.name == "DurCo")).one()
        s.add(models.Visit(property_id=p.id, date=dt(2026, 7, 13).date(), status="done",
                           started_at=dt(2026, 7, 13, 13, 0),
                           completed_at=dt(2026, 7, 13, 13, 42)))
        s.commit()
    assert "42 min" in client.get(f"/customers/{cid}").text

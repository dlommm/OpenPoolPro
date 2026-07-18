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

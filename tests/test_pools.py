from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _prop(client, engine):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        return s.scalars(select(models.Property)).one().id


def test_create_pool_computes_gallons(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        pool = s.scalars(select(models.Pool)).one()
        assert pool.gallons == 16875


def test_invalid_dims_rejected(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "0", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    assert r.status_code == 400 and "dimensions" in r.text


def test_gallons_preview(client, engine):
    _prop(client, engine)
    r = client.get("/pools/gallons-preview",
                   params={"shape": "rectangle", "length_ft": "30",
                           "width_ft": "15", "avg_depth_ft": "5"})
    assert "16,875" in r.text and "$55.00" in r.text
    r = client.get("/pools/gallons-preview",
                   params={"shape": "rectangle", "length_ft": "", "width_ft": "", "avg_depth_ft": ""})
    assert r.status_code == 200  # never 500 while typing


def test_equipment_crud(client, engine):
    pid = _prop(client, engine)
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "round", "length_ft": "20", "width_ft": "",
        "avg_depth_ft": "4", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id
    client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "pump", "brand": "Pentair", "model": "SuperFlo", "serial": "X1",
        "install_date": "", "notes": ""})
    with Session(engine) as s:
        eq = s.scalars(select(models.Equipment)).one()
        assert eq.brand == "Pentair"


def test_missing_ids_return_404(client, engine):
    assert client.get("/properties/999/pools/new").status_code == 404
    assert client.get("/pools/999/edit").status_code == 404
    assert client.post("/pools/999/deactivate").status_code == 404
    assert client.post("/pools/999/equipment/new", data={
        "kind": "pump", "brand": "", "model": "", "serial": "",
        "install_date": "", "notes": ""}).status_code == 404
    assert client.post("/equipment/999/delete").status_code == 404


def test_missing_form_fields_return_400_not_500(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={"shape": "rectangle"})
    assert r.status_code == 400

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

EXPORT_HEADER = ("customer_name,qb_display_name,phone,email,customer_notes,"
                 "property_label,address,gate_code,dog_note,frequency,weekday,"
                 "rate_override_dollars,assigned_tech,pool_shape,length_ft,width_ft,"
                 "avg_depth_ft,water_type")


def _seed_full(client, engine):
    client.post("/customers/new", data={"name": "Csv Family", "qb_display_name": "Csv LLC",
                                        "phone": "555-1", "email": "c@x.com", "notes": "vip"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "1 Way", "gate_code": "77", "dog_note": "",
        "notes": "", "frequency": "biweekly", "weekday": "2", "anchor_date": "2026-07-08",
        "rate_override_dollars": "62.50", "assigned_tech_id": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})
    return cid


def test_export_shape(client, engine):
    _seed_full(client, engine)
    r = client.get("/customers/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == EXPORT_HEADER
    row = lines[1]
    assert "Csv Family" in row and "62.50" in row and "rectangle" in row and "biweekly" in row


def test_export_admin_only(client, tech_client):
    t = tech_client(name="nope", manage_customers=True)
    assert t.get("/customers/export", follow_redirects=False).status_code == 403

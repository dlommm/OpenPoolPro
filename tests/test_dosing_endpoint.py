from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed(client, engine, water_type="chlorine", with_pool=True):
    client.post("/customers/new", data={"name": "DoseCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    if with_pool:
        client.post(f"/properties/{pid}/pools/new", data={
            "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
            "avg_depth_ft": "5", "water_type": water_type, "surface": "plaster",
            "notes": ""})
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.cost_per_unit_cents = 450  # cost must NEVER leak into the fragment
        s.commit()
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        return s.scalars(select(models.Visit)).one().id


def test_dose_fragment_happy_path(client, engine):
    vid = seed(client, engine)
    r = client.get(f"/visit/{vid}/doses", params={"fc": "2.0"})
    assert r.status_code == 200
    assert "Liquid chlorine" in r.text and "0.2" in r.text and "gal" in r.text
    assert "$" not in r.text
    assert 'data-amount' in r.text  # applyable dose carries data attrs for Apply


def test_dose_fragment_balanced(client, engine):
    vid = seed(client, engine)
    r = client.get(f"/visit/{vid}/doses", params={"fc": "4.0", "ph": "7.5"})
    assert "balanced" in r.text.lower()


def test_dose_fragment_no_readings(client, engine):
    vid = seed(client, engine)
    r = client.get(f"/visit/{vid}/doses")
    assert r.status_code == 200 and "Enter readings" in r.text


def test_dose_fragment_garbage_never_500(client, engine):
    vid = seed(client, engine)
    r = client.get(f"/visit/{vid}/doses", params={"fc": "abc", "ph": "inf", "ta": "-"})
    assert r.status_code == 200


def test_dose_fragment_no_pool_hint(client, engine):
    vid = seed(client, engine, with_pool=False)
    r = client.get(f"/visit/{vid}/doses", params={"fc": "2.0"})
    assert r.status_code == 200 and "Add a pool" in r.text


def test_dose_fragment_salt_pool(client, engine):
    vid = seed(client, engine, water_type="salt")
    r = client.get(f"/visit/{vid}/doses", params={"salt": "2800"})
    assert "Salt" in r.text


def test_dose_fragment_missing_visit_404(client, engine):
    assert client.get("/visit/999/doses", params={"fc": "2"}).status_code == 404


def test_visit_form_wires_dose_panel(client, engine):
    vid = seed(client, engine)
    with Session(engine) as s:
        pid = s.scalars(select(models.Visit)).one().property_id
    html = client.get(f"/visit/{pid}/2026-07-13").text
    assert f'hx-get="/visit/{vid}/doses"' in html
    assert 'id="dose-panel"' in html
    assert 'data-chem-input=' in html
    assert "Suggested doses" in html

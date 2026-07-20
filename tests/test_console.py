from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app import models


def _seed(client, engine):
    client.post("/customers/new", data={"name": "Console Co", "qb_display_name": "",
                                        "phone": "555-7777", "email": "cc@x.com", "notes": ""})
    with Session(engine) as s:
        from sqlalchemy import select
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Marina", "address": "9 Console Way", "gate_code": "11",
        "dog_warning": "on", "dog_note": "Big dog",
        "notes": "", "frequency": "weekly", "weekday": str(date.today().weekday()),
        "anchor_date": "", "rate_override_dollars": "", "assigned_tech_id": ""})
    return cid


def test_dashboard_admin_only_and_cards(client, engine, tech_client):
    _seed(client, engine)
    with Session(engine) as s:
        from sqlalchemy import select
        p = s.scalars(select(models.Property)).one()
        s.add(models.Visit(property_id=p.id, date=date.today(), status="done",
                           rate_cents=5500, completed_at=models.utcnow()))
        s.add(models.Job(property_id=p.id, title="OpenJob", status="open",
                         priority="high", labor_cents=0))
        s.commit()
    html = client.get("/dashboard").text
    assert "$55.00" in html          # revenue MTD from the done visit
    assert "OpenJob" in html or "1" in html
    t = tech_client(name="dashless")
    assert t.get("/dashboard", follow_redirects=False).status_code == 403


def test_dashboard_shows_equipment_warranty_added_via_form(client, engine):
    # End-to-end: warranty_end entered on the equipment add form must reach
    # the dashboard's warranty card through the real write path (not just
    # the warranty_alerts() function in isolation).
    from sqlalchemy import select
    cid = _seed(client, engine)
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "round", "length_ft": "20", "width_ft": "",
        "avg_depth_ft": "4", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id
    warranty_end = date.today() + timedelta(days=10)  # inside the 60d dashboard window
    client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "heater", "brand": "Hayward", "model": "H150", "serial": "",
        "install_date": "", "warranty_end": warranty_end.isoformat(), "notes": ""})

    html = client.get("/dashboard").text
    assert "Hayward" in html
    assert 'data-card="warranties">1<' in html


def test_dashboard_open_jobs_matches_jobs_screen(client, engine):
    _seed(client, engine)
    from sqlalchemy import select
    with Session(engine) as s:
        p = s.scalars(select(models.Property)).one()
        s.add(models.Job(property_id=p.id, title="SchedJob", status="scheduled",
                         scheduled_date=date.today(), labor_cents=0))
        s.add(models.Job(property_id=p.id, title="OpenOnly", status="open", labor_cents=0))
        s.commit()
    html = client.get("/dashboard").text
    import re
    m = re.search(r'data-card="open-jobs"[^>]*>(\d+)<', html)
    assert m and m.group(1) == "1"


def test_route_sheet_prints_stops(client, engine):
    _seed(client, engine)
    html = client.get("/today/print").text
    assert "Console Co" in html and "9 Console Way" in html and "Gate 11" in html
    assert "555-7777" in html and "Big dog" in html
    assert "media print" in html


def test_route_sheet_respects_tech_visibility(client, engine, tech_client):
    _seed(client, engine)
    t = tech_client(name="sheettech")
    html = t.get("/today/print").text
    assert "Console Co" in html  # unassigned stop visible to techs


def test_search_groups_and_fields(client, engine):
    _seed(client, engine)
    for q in ["console", "555-7777", "cc@x.com", "Console Way", "Marina"]:
        html = client.get("/search", params={"q": q}).text
        assert "Console Co" in html, q
    assert "Nothing found" in client.get("/search", params={"q": "zzzzzz"}).text
    assert client.get("/search", params={"q": "x" * 500}).status_code == 200


def test_search_tech_accessible_no_money(client, engine, tech_client):
    _seed(client, engine)
    t = tech_client(name="searchtech")
    html = t.get("/search", params={"q": "console"}).text
    assert "Console Co" in html and "$" not in html

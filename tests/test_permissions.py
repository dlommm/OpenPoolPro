import pytest

ADMIN_ONLY = [
    ("get", "/settings"),
    ("post", "/settings/tiers"),
    ("post", "/settings/chemicals"),
    ("post", "/settings/tasks"),
    ("post", "/settings/general"),
    ("post", "/settings/job_templates"),
]
NEEDS_MANAGE = [
    ("get", "/customers/new"),
    ("post", "/customers/new"),
    ("get", "/customers/1/edit"),
    ("post", "/customers/1/deactivate"),
    ("post", "/customers/1/reactivate"),
    ("post", "/customers/1/tags/add"),
    ("post", "/customers/1/tags/remove"),
    ("post", "/customers/1/comm/add"),
    ("get", "/customers/1/properties/new"),
    ("get", "/properties/1/edit"),
    ("post", "/properties/1/reactivate"),
    ("post", "/properties/1/holds/new"),
    ("post", "/properties/1/holds/1/end"),
    ("post", "/properties/1/tasks/new"),
    ("post", "/properties/1/tasks/1/deactivate"),
    ("post", "/properties/1/tasks/1/move"),
    ("post", "/properties/1/rules/new"),
    ("post", "/properties/1/rules/1/edit"),
    ("post", "/properties/1/rules/1/deactivate"),
    ("get", "/properties/1/pools/new"),
    ("get", "/pools/1/edit"),
    ("post", "/pools/1/deactivate"),
    ("get", "/quotes"),
    ("get", "/quotes/new"),
    ("post", "/quotes/new"),
    ("get", "/quotes/1"),
    ("post", "/quotes/1/edit"),
    ("post", "/quotes/1/status"),
    ("post", "/quotes/1/convert"),
    ("post", "/quotes/1/duplicate"),
    ("post", "/quotes/1/lines/from-template"),
]
NEEDS_MONEY = [("get", "/money"), ("get", "/money/export")]


def _seed_customer(client):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    client.post("/customers/1/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""})
    client.post("/properties/1/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})


@pytest.mark.parametrize("method,path", ADMIN_ONLY + NEEDS_MANAGE + NEEDS_MONEY)
def test_bare_tech_gets_403(client, tech_client, method, path):
    _seed_customer(client)
    t = tech_client(name="bare")
    r = getattr(t, method)(path, follow_redirects=False)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_flags_open_their_doors(client, tech_client):
    _seed_customer(client)
    t = tech_client(name="manager", manage_customers=True, view_money=True)
    assert t.get("/customers/new", follow_redirects=False).status_code == 200
    assert t.get("/money", follow_redirects=False).status_code == 200
    assert t.get("/settings", follow_redirects=False).status_code == 403  # still not admin


def test_tech_can_still_work(client, tech_client):
    _seed_customer(client)
    t = tech_client(name="worker")
    assert t.get("/", follow_redirects=False).status_code == 200
    assert t.get("/week", follow_redirects=False).status_code == 200
    assert t.get("/customers", follow_redirects=False).status_code == 200
    assert t.get("/customers/1", follow_redirects=False).status_code == 200
    assert t.get("/visit/1/2026-07-13", follow_redirects=False).status_code == 200
    assert t.get("/jobs", follow_redirects=False).status_code == 200


def test_money_hidden_from_rendered_html(client, tech_client):
    _seed_customer(client)
    t = tech_client(name="nomoney")
    html = t.get("/customers/1").text
    assert "$" not in html          # no rates anywhere
    assert 'href="/money"' not in html
    visit_html = t.get("/visit/1/2026-07-13").text
    assert "/visit" in visit_html and "$" not in visit_html


def test_admin_sees_money_everywhere(client):
    _seed_customer(client)
    html = client.get("/customers/1").text
    assert "$" in html and 'href="/money"' in html


def test_job_form_hides_and_preserves_labor(client, tech_client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    _seed_customer(client)
    client.post("/jobs/new", data={"property_id": "1", "title": "Priced job",
                                   "description": "", "priority": "normal",
                                   "labor_dollars": "425", "scheduled_date": "",
                                   "job_type": "service", "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    t = tech_client(name="fieldtech")
    html = t.get(f"/jobs/{jid}/edit").text
    assert "$" not in html and "Labor" not in html
    t.post(f"/jobs/{jid}/edit", data={"property_id": "1", "title": "Renamed",
                                      "description": "", "priority": "normal",
                                      "scheduled_date": ""})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.title == "Renamed" and j.labor_cents == 42500  # preserved


def test_gallons_preview_gated_and_money_scoped(client, tech_client):
    params = {"shape": "rectangle", "length_ft": "30", "width_ft": "15", "avg_depth_ft": "5"}
    bare = tech_client(name="bare2")
    assert bare.get("/pools/gallons-preview", params=params,
                    follow_redirects=False).status_code == 403
    mgr = tech_client(name="mgr2", manage_customers=True)
    r = mgr.get("/pools/gallons-preview", params=params)
    assert "16,875" in r.text and "$" not in r.text
    admin = client.get("/pools/gallons-preview", params=params)
    assert "$55.00" in admin.text


def test_property_form_rate_hidden_and_preserved(client, tech_client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    _seed_customer(client)
    client.post("/properties/1/edit", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "88", "assigned_tech_id": ""})
    t = tech_client(name="mgrnomoney", manage_customers=True)
    html = t.get("/properties/1/edit").text
    assert "$" not in html and "88" not in html
    t.post("/properties/1/edit", data={
        "label": "Home2", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "1", "assigned_tech_id": ""})
    with Session(engine) as s:
        p = s.get(models.Property, 1)
        assert p.label == "Home2" and p.rate_override_cents == 8800  # preserved


def test_create_job_labor_zeroed_for_bare_tech(client, tech_client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    _seed_customer(client)
    t = tech_client(name="laborless")
    t.post("/jobs/new", data={"property_id": "1", "title": "Sneaky", "description": "",
                              "priority": "normal", "labor_dollars": "999",
                              "scheduled_date": "", "job_type": "service",
                              "assigned_tech_id": ""})
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Sneaky")).one()
        assert j.labor_cents == 0

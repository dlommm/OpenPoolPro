import pytest

ADMIN_ONLY = [
    ("get", "/settings"),
    ("post", "/settings/tiers"),
    ("post", "/settings/chemicals"),
    ("post", "/settings/tasks"),
    ("post", "/settings/general"),
]
NEEDS_MANAGE = [
    ("get", "/customers/new"),
    ("post", "/customers/new"),
    ("get", "/customers/1/edit"),
    ("post", "/customers/1/deactivate"),
    ("get", "/customers/1/properties/new"),
    ("get", "/properties/1/edit"),
    ("get", "/properties/1/pools/new"),
    ("get", "/pools/1/edit"),
    ("post", "/pools/1/deactivate"),
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

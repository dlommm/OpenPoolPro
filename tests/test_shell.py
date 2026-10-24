def test_manifest_and_sw_served(client):
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_dollars_filter():
    from app.main import templates
    dollars = templates.env.filters["dollars"]
    assert dollars(123456) == "$1,234.56"
    assert dollars(0) == "$0.00"
    assert dollars(None) == "$0.00"


def test_base_template_blocks_and_nav(client):
    from app.main import templates
    tpl = templates.env.from_string(
        '{% extends "base.html" %}{% block title %}T-Probe{% endblock %}'
        '{% block content %}C-Probe{% endblock %}')
    html = tpl.render(request=None)
    assert "T-Probe" in html and "C-Probe" in html
    for href in ["/", "/week", "/customers", "/jobs", "/money", "/settings"]:
        assert f'href="{href}"' in html


def test_masthead_is_the_default_navigation(client):
    html = client.get("/").text
    assert '<body class="nav-top hdr-lit">' in html
    assert 'class="mast"' in html and 'class="side"' not in html
    assert 'id="menu-sheet"' in html  # tablet/phone menu
    assert 'class="bnav"' in html     # phone bottom bar
    assert 'action="/search"' in html
    assert 'action="/logout"' in html


def test_sidebar_layout_is_an_admin_setting(client, engine):
    from sqlalchemy.orm import Session

    from app import models
    r = client.post("/settings/appearance", data={"nav_layout": "side"}, follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/").text
    assert "nav-side" in html and 'class="side"' in html
    # garbage keeps the stored value
    client.post("/settings/appearance", data={"nav_layout": "sideways", "header_style": "x"})
    with Session(engine) as s:
        assert models.get_setting(s, "nav_layout", "") == "side"
        assert models.get_setting(s, "header_style", "lit") == "lit"


def test_active_page_is_marked_once(client):
    html = client.get("/customers").text
    assert html.count('class="sn on"') == 1  # Customers group: second row
    assert 'class="mn on"' in html


def test_login_page_has_no_staff_chrome(client, engine, monkeypatch):
    from fastapi.testclient import TestClient

    import app.config as config
    from app.main import create_app
    monkeypatch.setattr(config, "APP_PASSWORD", "x-unused-1")
    c = TestClient(create_app(engine=engine))
    html = c.get("/login").text
    assert 'class="mast"' not in html and "bare-shell" in html
    assert 'class="mast"' in client.get("/").text

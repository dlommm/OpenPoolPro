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


def test_sidebar_and_tabbar_coexist(client):
    html = client.get("/").text
    assert 'id="sidebar"' in html and "hidden lg:flex" in html
    assert 'id="tabbar"' in html and "lg:hidden" in html
    assert 'action="/search"' in html

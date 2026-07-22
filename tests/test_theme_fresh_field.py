"""Fresh-Field theme integration smoke test.

Sets active_theme = "fresh-field" (read per request by the auth middleware),
then renders the four bespoke hero screens as the logged-in owner (admin, so
money is visible). Each must return 200, carry the theme's data-theme flag,
and show a bespoke `ff-` marker proving the theme's template/CSS took effect.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _activate_fresh_field(engine):
    with Session(engine) as s:
        models.set_setting(s, "active_theme", "fresh-field")
        s.commit()


def _make_customer(client, engine):
    client.post("/customers/new", data={
        "name": "Fresh Field Co", "qb_display_name": "", "phone": "555-2020",
        "email": "ff@example.com", "notes": ""})
    with Session(engine) as s:
        return s.scalars(select(models.Customer)).one().id


def test_fresh_field_theme_renders_all_hero_screens(client, engine):
    _activate_fresh_field(engine)
    cid = _make_customer(client, engine)

    cases = [
        ("/", "ff-progress-card"),          # Route: water-fill ring
        ("/dashboard", "ff-hero-rev"),      # Dashboard: gradient revenue hero
        (f"/customers/{cid}", "ff-cust-hero"),  # Customer 360
        ("/money", "ff-hero-rev"),          # Money: gradient billable hero
    ]
    for url, marker in cases:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        html = r.text
        assert 'data-theme="fresh-field"' in html, f"{url} missing data-theme"
        assert marker in html, f"{url} missing bespoke marker {marker!r}"
        # No server-side render error leaked into the page.
        assert "Internal Server Error" not in html
        assert "jinja2" not in html.lower()


def test_fresh_field_customer_detail_has_custabs(client, engine):
    """Customer detail keeps its ff-cust-hero marker AND gets the shared
    Profile/Photos/Pool tab bar wired in the fresh-field visual idiom."""
    _activate_fresh_field(engine)
    cid = _make_customer(client, engine)
    body = client.get(f"/customers/{cid}").text
    assert "ff-cust-hero" in body          # customer hero marker preserved
    for name in ("profile", "photos", "pool"):
        assert f'data-custab-btn="{name}"' in body
        assert f'data-custab="{name}"' in body


def test_fresh_field_chrome_and_money_gating(client, engine):
    _activate_fresh_field(engine)

    # Base chrome ships the theme's bespoke sync pill on every page.
    home = client.get("/").text
    assert "ff-sync" in home

    # Owner (admin) sees money on the money page.
    money = client.get("/money").text
    assert "ff-billing" in money or "Nothing billable" in money

"""Warm-Coastal theme: the four bespoke hero screens render under the active
theme, bind real router context, and carry the theme's `wc-` signature classes.

Uses the shared `client` fixture (logged-in owner) + `engine`. The owner sees
money and all routes, so every hero route is reachable.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _set_theme(engine, theme):
    with Session(engine) as s:
        models.set_setting(s, "active_theme", theme)
        s.commit()


def test_warm_coastal_hero_screens_render_with_signature(client, engine):
    _set_theme(engine, "warm-coastal")

    # Seed a customer so /customers/<id> resolves to the detail hero.
    client.post("/customers/new", data={
        "name": "Tidewater Co", "qb_display_name": "", "phone": "555-2020",
        "email": "tide@example.com", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id

    # route -> a bespoke wc- marker class unique to that hero template.
    markers = {
        "/": "wc-tide",                  # Route: rising tide-line gauge
        "/dashboard": "wc-hero-rev",     # Dashboard: revenue hero
        f"/customers/{cid}": "wc-cust-hero",  # Customer 360 hero
        "/money": "wc-sum",              # Money: summary sum-cards
    }

    for path, marker in markers.items():
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        html = r.text
        # Active theme reflected on <html> (theme-aware loader picked the theme base).
        assert 'data-theme="warm-coastal"' in html, path
        # The bespoke hero template rendered (not the shared fallback).
        assert marker in html, f"{path} missing marker {marker!r}"
        # The shared horizon signature is present on every warm-coastal hero.
        assert "wc-horizon" in html, path


def test_warm_coastal_today_preserves_progress_and_maps(client, engine):
    """Route hero keeps the load-bearing '<done> of <total> done' progress
    string and the Google-Maps fallback link that the app depends on."""
    from datetime import date, timedelta

    _set_theme(engine, "warm-coastal")
    with Session(engine) as s:
        c = models.Customer(name="RouteGuy")
        p = models.Property(customer=c, label="Home", address="9 Palm St",
                            frequency="weekly", weekday=date.today().weekday(),
                            anchor_date=date.today() - timedelta(days=28), route_order=1)
        s.add(p)
        s.commit()

    html = client.get("/").text
    assert "0 of 1 done" in html
    assert "maps.google.com" in html
    assert "RouteGuy" in html and "9 Palm St" in html

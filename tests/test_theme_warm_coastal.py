"""Warm-Coastal theme: the four bespoke hero screens render under the active
theme, bind real router context, and carry the theme's `wc-` signature classes.

Uses the shared `client` fixture (logged-in owner) + `engine`. The owner sees
money and all routes, so every hero route is reachable.
"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

_WC_CSS = Path(__file__).resolve().parents[1] / "css" / "themes" / "warm-coastal.css"


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


def test_warm_coastal_customer_detail_has_custabs(client, engine):
    """Customer detail keeps its wc- 360 hero AND gets the shared
    Profile/Photos/Pool tab bar wired in the warm-coastal visual idiom."""
    _set_theme(engine, "warm-coastal")
    client.post("/customers/new", data={
        "name": "Marlin House", "qb_display_name": "", "phone": "555-3030",
        "email": "marlin@example.com", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    body = client.get(f"/customers/{cid}").text
    assert "wc-cust-hero" in body          # 360 hero marker preserved
    for name in ("profile", "photos", "pool"):
        assert f'data-custab-btn="{name}"' in body
        assert f'data-custab="{name}"' in body


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


def test_warm_coastal_default_logo_is_openpoolpro_brand(client, engine):
    """With no custom uploaded logo, the built-in default is the OpenPoolPro
    brand mark + wordmark (not the old invented wave/"Coastline" fallback)."""
    _set_theme(engine, "warm-coastal")
    html = client.get("/").text

    # Emblem asset + themed wordmark render (desktop + mobile fallbacks).
    assert "/static/brand/openpoolpro-mark.png" in html
    assert 'OpenPool<b class="text-pool">Pro</b>' in html
    # The old invented fallback wordmark/sub-label is gone.
    assert "wc-brand-sub" not in html
    assert ">Coastline<" not in html
    # The custom-logo override branch is left intact in the template source
    # (it only renders when a logo has actually been uploaded).
    base_src = (Path(__file__).resolve().parents[1] / "app" / "templates"
                / "themes" / "warm-coastal" / "base.html").read_text()
    assert base_src.count('src="/branding/logo"') == 2
    assert "has_logo_check()" in base_src


def test_warm_coastal_tide_overlay_text_is_legible_at_any_fill(engine):
    """All tide-overlay text is off-white and backed by a dark bottom scrim so
    it reads at 0 fill (pale sand) and at full fill (deep water)."""
    css = _WC_CSS.read_text()

    # Dark bottom scrim pseudo-element on .wc-tide (under text, over sand/water).
    assert ".wc-tide::after" in css
    assert "linear-gradient(to top, rgba(11, 42, 51, 0.62), transparent)" in css

    # The remain block is no longer dark ink (which went dark-on-dark at high water).
    remain = css.split(".wc-tide-remain {", 1)[1].split("}", 1)[0]
    assert "var(--color-ink)" not in remain
    assert "#FBF7EF" in remain
    # Overlay number/label stay off-white.
    overlay = css.split(".wc-tide-overlay {", 1)[1].split("}", 1)[0]
    assert "#FBF7EF" in overlay


def test_warm_coastal_skipped_stop_is_de_emphasized(client, engine):
    """A skipped stop gets a muted/gray band (shared `stop-done`) + a distinct
    "Skipped" pill — it must NOT read like a live/pending "due" stop."""
    from datetime import date, timedelta

    _set_theme(engine, "warm-coastal")
    with Session(engine) as s:
        c = models.Customer(name="SkipCo")
        p = models.Property(customer=c, label="Home", address="7 Reef Rd",
                            frequency="weekly", weekday=date.today().weekday(),
                            anchor_date=date.today() - timedelta(days=28), route_order=1)
        s.add(p)
        s.flush()
        s.add(models.Visit(property_id=p.id, date=date.today(), status="skipped"))
        s.commit()

    html = client.get("/").text
    # Card carries the skipped de-emphasis modifiers (gray band via stop-done).
    assert "wc-stop-skipped" in html
    assert "stop-done wc-stop-skipped" in html
    # Distinct muted "Skipped" pill, not a filled active pill.
    assert "wc-status  wc-skipped" in html or "wc-skipped" in html
    assert "Skipped" in html
    # It is NOT treated as the live "next" stop (no cyan active band for it).
    assert "wc-stop-next" not in html

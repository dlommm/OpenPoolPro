"""Control-Room theme: the four bespoke hero screens render under the active
theme with real router context, no template error, and their signature `cr-`
markers present. Uses the shared `client` fixture (logged in as owner/admin, so
money-gated screens are visible)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _activate_control_room(engine):
    with Session(engine) as s:
        models.set_setting(s, "active_theme", "control-room")
        s.commit()


def _seed_customer(client, engine):
    client.post("/customers/new", data={
        "name": "Vega Residence", "qb_display_name": "", "phone": "555-2020",
        "email": "v@x.com", "notes": "Saltwater · Tue route"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Beach house", "street": "44 Pelican Way", "gate_code": "1180",
        "dog_note": "", "notes": "", "frequency": "weekly", "weekday": "1",
        "anchor_date": "", "rate_override_dollars": ""})
    return cid


def test_control_room_hero_screens_render(client, engine):
    _activate_control_room(engine)
    cid = _seed_customer(client, engine)

    # path -> a bespoke marker unique to that hero template
    checks = {
        "/": "cr-gauge-ring",              # Route: segmented gauge ring
        "/dashboard": "cr-hero",           # Dashboard: amber money hero
        f"/customers/{cid}": "cr-cust-id", # Customer: identity card
        "/money": "cr-msum",               # Money: mono summary tiles
    }
    for path, marker in checks.items():
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        body = r.text
        assert 'data-theme="control-room"' in body, f"{path}: theme not active"
        assert marker in body, f"{path}: missing marker {marker!r}"
        # offline-queue indicator IDs preserved by the theme base chrome
        assert 'id="sync-badge"' in body, f"{path}: sync badge id missing"
        low = body.lower()
        assert "traceback" not in low, f"{path}: server error leaked"
        assert "internalservererror" not in low.replace(" ", ""), f"{path}: 500 leaked"


def test_control_room_route_signatures_and_maps_links(client, engine):
    """Route hero keeps the dispatch rail + Maps deep-links intact."""
    _activate_control_room(engine)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "cr-rail" in body            # vertical dispatch rail
    assert "cr-rule" in body            # ticked cyan title rule
    assert "maps.apple.com" in body or "No stops today" in body


def test_control_room_dashboard_keeps_console_hooks(client, engine):
    """The control-room dashboard must preserve the load-bearing data-card
    hooks the shared dashboard exposes (test_console.py asserts on both)."""
    _activate_control_room(engine)
    _seed_customer(client, engine)
    r = client.get("/dashboard")
    assert r.status_code == 200
    body = r.text
    assert 'data-theme="control-room"' in body
    assert 'data-card="open-jobs"' in body
    assert 'data-card="warranties"' in body


def test_control_room_money_preserves_export_and_nav(client, engine):
    """Money hero keeps the export action, month nav, and the mono invoice
    table once there is a billable row."""
    from app.routers.today import business_today

    _activate_control_room(engine)
    cid = _seed_customer(client, engine)
    d = business_today()
    with Session(engine) as s:
        pid = s.scalars(select(models.Property).where(
            models.Property.customer_id == cid)).first().id
        s.add(models.Visit(property_id=pid, date=d, status="done", rate_cents=14500))
        s.commit()

    r = client.get(f"/money?ym={d.year}-{d.month:02d}")
    assert r.status_code == 200
    body = r.text
    assert "/money/export?ym=" in body   # QuickBooks export preserved
    assert "/money?ym=" in body          # prev/next month nav preserved
    assert "cr-inv" in body              # mono invoice-style table (rows present)
    assert "Vega Residence" in body      # real rollup data bound

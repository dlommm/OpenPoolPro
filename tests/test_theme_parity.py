"""Guard against themed templates silently dropping data the router computed.

`ThemeAwareLoader` overrides whole files, not styles, so five pages exist as
four near-total rewrites. Nothing forces those copies to render the same
values, and drift has already shipped: `app/routers/money.py` computed
`grand_tax`/`grand_total` and only the shared `money.html` displayed them —
but all three allowed themes override that page, so the shared copy never
renders in production and the sales-tax breakdown was invisible to every user.

These tests render the same URL under every theme and assert the figures
appear in all of them.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.main import ALLOWED_THEMES


def _set_theme(engine, theme):
    with Session(engine) as s:
        models.set_setting(s, "active_theme", theme)
        s.commit()


def _seed_taxed_month(engine):
    """One completed visit in a taxed state, so the Money page has tax to show."""
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        c = models.Customer(name="Taxable Co", active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", address="1 St", state="NJ",
                            active=True, weekday=0, frequency="weekly", route_order=1,
                            rate_override_cents=20000)
        s.add(p)
        s.flush()
        s.add(models.Visit(property_id=p.id, date=today, status="done",
                           rate_cents=20000, tax_rate_ppm=66250))
        row = s.get(models.TaxRate, "NJ")
        if row is None:
            s.add(models.TaxRate(state="NJ", rate_ppm=66250))
        else:
            row.rate_ppm = 66250
        models.set_setting(s, "sales_tax_enabled", "1")
        models.set_setting(s, "tax_weekly", "1")
        s.commit()
    return today


@pytest.mark.parametrize("theme", sorted(ALLOWED_THEMES))
def test_money_page_shows_sales_tax_in_every_theme(client, engine, theme):
    today = _seed_taxed_month(engine)
    _set_theme(engine, theme)

    body = client.get(f"/money?ym={today.year}-{today.month:02d}").text
    assert "Sales tax" in body, f"theme {theme!r} does not render the sales-tax line"
    assert "Grand total" in body, f"theme {theme!r} does not render the grand total"
    # $200.00 labor at 6.625% = $13.25 of tax, $213.25 all in.
    assert "$13.25" in body, f"theme {theme!r} renders no tax amount"
    assert "$213.25" in body, f"theme {theme!r} renders no grand total amount"


@pytest.mark.parametrize("theme", sorted(ALLOWED_THEMES))
def test_money_page_hides_tax_when_disabled(client, engine, theme):
    """With tax off, every theme must look exactly as it did before tax existed."""
    _seed_taxed_month(engine)
    with Session(engine) as s:
        models.set_setting(s, "sales_tax_enabled", "0")
        s.commit()
    _set_theme(engine, theme)

    from app.routers.today import business_today
    today = business_today()
    body = client.get(f"/money?ym={today.year}-{today.month:02d}").text
    assert "Sales tax" not in body
    assert "Grand total" not in body


@pytest.mark.parametrize("theme", sorted(ALLOWED_THEMES))
def test_core_pages_render_under_every_theme(client, engine, theme):
    """Smoke: a theme override with a syntax error or a bad extends only shows
    up when that theme is active, which is exactly when a user finds it."""
    _seed_taxed_month(engine)
    _set_theme(engine, theme)
    for url in ("/", "/week", "/customers", "/money", "/dashboard"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 200, f"{url} returned {r.status_code} under {theme!r}"


def test_every_allowed_theme_is_actually_installed():
    """A theme in ALLOWED_THEMES with no template directory silently falls back
    to the shared pages, which is how the money-tax drift stayed invisible."""
    import pathlib
    root = pathlib.Path("app/templates/themes")
    for theme in ALLOWED_THEMES:
        assert (root / theme).is_dir(), f"{theme} listed in ALLOWED_THEMES but has no templates"


def test_theme_overrides_do_not_lose_context_keys(engine):
    """Any page a theme overrides must reference every money-bearing context key
    the shared template does. Catches the next `grand_tax` before it ships."""
    import pathlib
    import re
    root = pathlib.Path("app/templates")
    tracked = re.compile(r"\{\{\s*(grand\w*|collect\.\w+)\b")
    failures = []
    for shared in root.glob("*.html"):
        keys = set(tracked.findall(shared.read_text()))
        if not keys:
            continue
        for theme in sorted(ALLOWED_THEMES):
            override = root / "themes" / theme / shared.name
            if not override.exists():
                continue
            missing = keys - set(tracked.findall(override.read_text()))
            if missing:
                failures.append(f"themes/{theme}/{shared.name} never renders {sorted(missing)}")
    assert not failures, "themed pages drop values the shared page shows:\n  " + "\n  ".join(failures)

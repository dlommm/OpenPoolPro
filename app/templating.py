"""The Jinja singleton, appearance settings, and template filters.

Extracted from app/main.py: while `templates` lived there, every router's
`from app.main import templates` made main un-importable from any router —
so main had to defer all 23 router imports into the create_app body, and any
service module touching a router risked a boot-breaking cycle. Routers (and
main) now import from here; nothing in this module imports the web layer.
"""
import logging
import os
from datetime import date, datetime

import jinja2
from fastapi.templating import Jinja2Templates

from app import config, timefmt

log = logging.getLogger("openpoolpro.app")

# --- Appearance -----------------------------------------------------------
# One theme (Aqua Atelier, light and dark). What the company can still choose,
# org-wide, is how the shell is laid out: navigation in the deep-water
# masthead ("top") or in a sidebar ("side", desktop only -- tablet and phone
# always use the masthead), and how lively that masthead is. Read once per
# request in the auth middleware and exposed to templates as request.state.ui.
NAV_LAYOUTS = ("top", "side")
HEADER_STYLES = ("lit", "still", "solid")
UI_DEFAULTS = {"nav_layout": "top", "header_style": "lit", "company": ""}


def ui_settings(session) -> dict:
    """The company-wide appearance choices, each validated against its allowed
    set so a hand-edited or stale setting falls back instead of reaching
    markup."""
    from app import models

    nav = models.get_setting(session, "nav_layout", UI_DEFAULTS["nav_layout"])
    hdr = models.get_setting(session, "header_style", UI_DEFAULTS["header_style"])
    return {"nav_layout": nav if nav in NAV_LAYOUTS else UI_DEFAULTS["nav_layout"],
            "header_style": hdr if hdr in HEADER_STYLES else UI_DEFAULTS["header_style"],
            "company": models.get_setting(session, "business_name", "") or ""}


templates = Jinja2Templates(directory="app/templates")
# Bust cached CSS/JS on every release so a home-screen PWA fetches new assets.
templates.env.globals["app_version"] = config.APP_VERSION
templates.env.filters["dollars"] = lambda cents: f"${(cents or 0) / 100:,.2f}"
templates.env.filters["initials"] = lambda name: "".join(
    w[0] for w in (name or "?").split()[:2]).upper()
templates.env.filters["hhmm"] = timefmt.to_local_hhmm
templates.env.filters["localdt"] = lambda dt: (
    timefmt.to_local(dt).strftime("%Y-%m-%d %H:%M") if dt else "")
# One registration only. main.py used to register timefmt.to_local_date here
# and then silently overwrite it three lines later with timefmt.local_date —
# this keeps the winning (date-object) semantics but tolerates None, which
# to_local_date guarded against and local_date did not.
templates.env.filters["local_date"] = lambda dt: timefmt.local_date(dt) if dt else ""
def _month_name(ym) -> str:
    """'2026-09' -> 'September 2026'. A billing period is shown to a person the
    way the month picker beside it already shows it; the ISO form is what the
    form posts, not what a button should say."""
    try:
        year, month = str(ym).split("-")[:2]
        return date(int(year), int(month), 1).strftime("%B %Y")
    except (ValueError, TypeError):
        return str(ym or "")


def _short_date(value) -> str:
    """A calendar date as a person reads it: 'Sep 30, 2026'.

    Bare ISO belongs in form values and sort keys, not in a table cell a human
    scans. Anything unparseable is passed through rather than blanked, so a
    surprising value stays visible instead of silently vanishing.
    """
    if not value:
        return "—"
    try:
        if isinstance(value, str):
            value = date.fromisoformat(value[:10])
        return value.strftime("%b %-d, %Y")
    except (ValueError, TypeError, AttributeError):
        return str(value)


templates.env.filters["month_name"] = _month_name
def _stamp(value) -> str:
    """An ISO datetime string as a person reads it: 'Sep 16, 2026 1:00 AM'.

    For the operational timestamps where the time of day carries meaning --
    when a backup ran, when a token expires -- as opposed to short_date, which
    is for calendar dates where it does not.
    """
    if not value:
        return "Never recorded"
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            # Naive values are stored UTC, as localdt has always assumed.
            # Formatting one without converting would print UTC as if it were
            # the operator's wall clock -- a four-hour lie about when a thing
            # happened. Aware values already carry their offset.
            value = timefmt.to_local(value)
        return value.strftime("%b %-d, %Y %-I:%M %p")
    except (ValueError, TypeError, AttributeError):
        return str(value)


templates.env.filters["short_date"] = _short_date
templates.env.filters["stamp"] = _stamp
templates.env.filters["minutes_display"] = lambda m: (
    "" if m is None else (f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d}"))
templates.env.globals["has_logo_check"] = lambda: os.path.isfile(
    os.path.join(config.DATA_DIR, "branding", "logo.png"))


def _low_stock_count(request) -> int:
    """Nav-badge helper: short-lived session, mirrors has_logo_check's
    per-render global-check pattern so every page (via base.html) can show
    the count without every router threading it through its own context.

    Any failure (e.g. a locked SQLite database) is swallowed so a bad
    low-stock query never 500s every admin page; the badge just degrades
    to absent (0) for that render."""
    from sqlalchemy.orm import Session

    from app import inventory as inventory_module

    try:
        with Session(request.app.state.engine) as s:
            return len(inventory_module.low_stock(s))
    except Exception:
        # Still degrade to no badge, but say so: whatever breaks this query
        # (locked/corrupt DB, a migration half-applied) is one step away from
        # breaking real pages, and a badge that silently reads 0 looks
        # identical to genuinely healthy stock.
        log.warning("low-stock badge query failed; rendering without a badge",
                    exc_info=True)
        return 0


templates.env.globals["low_stock_count"] = _low_stock_count


@jinja2.pass_context
def _csp_nonce(ctx) -> str:
    """The per-request script nonce (set by the security_headers middleware).
    Every inline <script> must carry nonce="{{ csp_nonce() }}" — the CSP has
    no 'unsafe-inline' for scripts, so an un-nonced block silently does
    nothing. tests/test_csp_nonce.py greps the template sources to make that
    mistake impossible to ship."""
    request = ctx.get("request")
    return getattr(request.state, "csp_nonce", "") if request is not None else ""


templates.env.globals["csp_nonce"] = _csp_nonce


@jinja2.pass_context
def _staff_identity(ctx) -> str:
    """Id of the signed-in staff member, or "" when nobody is signed in.

    The service worker stamps this onto every offline-queued visit POST and
    replays it in an X-Queued-Owner header, so work queued by one tech can
    never be attributed to whoever happens to be signed in later on a shared
    device. Not a secret -- it is the same id the app already exposes in staff
    URLs -- and the server re-checks it on replay rather than trusting it.
    """
    request = ctx.get("request")
    user = getattr(request.state, "user", None) if request is not None else None
    return str(user.id) if user is not None else ""


templates.env.globals["staff_identity"] = _staff_identity


@jinja2.pass_context
def _nav(ctx):
    """The navigation for this request (see app/nav.py), with the badges the
    shell shows. Badge queries degrade to no badge, like low_stock_count."""
    from app import nav

    request = ctx.get("request")
    user = getattr(request.state, "user", None) if request is not None else None
    path = request.url.path if request is not None else "/"
    badges = {}
    if user is None or user.role == "admin":
        n = _low_stock_count(request) if request is not None else 0
        if n:
            badges["inventory"] = (str(n), "")
    return nav.build(user, path, badges)


templates.env.globals["nav"] = _nav

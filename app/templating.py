"""The Jinja singleton, theme resolution, and template filters.

Extracted from app/main.py: while `templates` lived there, every router's
`from app.main import templates` made main un-importable from any router —
so main had to defer all 23 router imports into the create_app body, and any
service module touching a router risked a boot-breaking cycle. Routers (and
main) now import from here; nothing in this module imports the web layer.
"""
import contextvars
import logging
import os
from datetime import date, datetime

import jinja2
from fastapi.templating import Jinja2Templates
from jinja2 import BaseLoader, FileSystemLoader, TemplateNotFound

from app import config, timefmt

log = logging.getLogger("openpoolpro.app")

# --- Multi-theme system --------------------------------------------------
# The active theme for the current request/task. Set once per request in the
# auth middleware; read by ThemeAwareLoader when resolving template names.
# A ContextVar is per-task, so concurrent requests never see each other's
# theme even though `templates` is a process-wide singleton.
ALLOWED_THEMES = {"warm-coastal", "control-room", "fresh-field"}
DEFAULT_THEME = "warm-coastal"
active_theme_var = contextvars.ContextVar("active_theme", default=DEFAULT_THEME)


class ThemeAwareLoader(BaseLoader):
    """Wraps a single FileSystemLoader and, for each lookup, prefers a
    theme-specific override before the shared template.

    For a request whose active theme is ``T``, ``get_source(env, "x.html")``
    first tries ``themes/T/x.html`` and, if that does not exist, falls back to
    the shared ``x.html``. Names already under ``themes/`` are looked up
    verbatim (so a theme's ``themes/T/base.html`` that other theme templates
    extend never re-prefixes into ``themes/T/themes/T/base.html``).

    This lets a file at ``app/templates/themes/<theme>/<name>`` override the
    shared ``app/templates/<name>`` at render time WITHOUT changing any of the
    router ``TemplateResponse(request, name, ctx)`` call sites.
    """

    def __init__(self, directory):
        self._fs = FileSystemLoader(directory)

    def get_source(self, environment, template):
        theme = active_theme_var.get()
        source = filename = uptodate = None
        if theme and not template.startswith("themes/"):
            try:
                source, filename, uptodate = self._fs.get_source(
                    environment, f"themes/{theme}/{template}")
            except TemplateNotFound:
                source = None
        if source is None:
            source, filename, uptodate = self._fs.get_source(environment, template)

        # Jinja caches compiled templates by NAME (e.g. "base.html"), which is
        # theme-blind. Without help, the first theme to render a name would
        # pin that name's cache entry and later themes would get the wrong
        # file. We resolved under `theme`, so report the cache entry stale the
        # moment the active theme differs -> Jinja re-invokes get_source and
        # re-resolves for the new theme. When the theme is unchanged we defer
        # to the file-mtime check (normal caching). The active theme is one
        # global admin setting, so in practice all concurrent requests share
        # it and this only re-resolves right after an admin switches.
        def theme_uptodate():
            if active_theme_var.get() != theme:
                return False
            return uptodate() if uptodate is not None else True

        return source, filename, theme_uptodate

    def list_templates(self):
        return self._fs.list_templates()


templates = Jinja2Templates(directory="app/templates")
# Bust cached CSS/JS on every release so a home-screen PWA fetches new assets.
templates.env.globals["app_version"] = config.APP_VERSION
# Swap the default FileSystemLoader for the theme-aware one on the shared
# singleton so every render (all 23 routers) goes through theme resolution.
templates.env.loader = ThemeAwareLoader("app/templates")
# auto_reload must stay on for the theme-aware `uptodate` (see ThemeAwareLoader)
# to invalidate the name-keyed template cache when the active theme changes.
templates.env.auto_reload = True
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

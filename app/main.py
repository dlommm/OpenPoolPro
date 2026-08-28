import contextvars
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import BaseLoader, FileSystemLoader, TemplateNotFound
from starlette.middleware.sessions import SessionMiddleware

from app import config, models, observability, seeds, timefmt  # noqa: F401
from app.db import Base, make_engine

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
# singleton so every render (all 22 routers) goes through theme resolution.
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
templates.env.filters["local_date"] = timefmt.to_local_date
templates.env.filters["minutes_display"] = lambda m: (
    "" if m is None else (f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d}"))
templates.env.filters["local_date"] = timefmt.local_date
templates.env.globals["has_logo_check"] = lambda: os.path.isfile(os.path.join(config.DATA_DIR, "branding", "logo.png"))


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


def create_app(engine=None) -> FastAPI:
    # First thing, so anything below (migrations, seeding, scheduler wiring)
    # that fails during boot fails somewhere an operator can read.
    observability.configure_logging()
    log.info("OpenPoolPro %s starting (tz=%s, scheduler=%s)",
             config.APP_VERSION, config.TIMEZONE, config.RUN_SCHEDULER)
    app = FastAPI(title="OpenPoolPro")
    # A real boot builds its own file-backed engine; the test suite always
    # injects an in-memory engine. Enforce secure secrets only on the former,
    # so a deployment can never run on the shipped placeholder SECRET_KEY.
    if engine is None:
        config.validate_secrets()
    app.state.engine = engine or make_engine()
    # Rate limiter for sensitive already-authenticated account actions (password
    # change, 2FA disable/regenerate) — a hijacked session shouldn't get
    # unlimited guesses at the current password or a 6-digit TOTP. App-scoped
    # (not a module singleton) so it never accumulates across the test suite.
    from app import security as _security
    app.state.account_limiter = _security.RateLimiter(max_fails=8, lock_seconds=900)
    Base.metadata.create_all(app.state.engine)
    from app import backup, migrate
    # Real boots snapshot the DB immediately before the first migration
    # statement that would change it (migrations DELETE/UPDATE rows, and the
    # nightly can be ~23h stale by upgrade time). Lazy on purpose: the common
    # nothing-to-migrate boot never pays for it. Test boots (injected
    # in-memory engine) have nothing worth vacuuming.
    pre_snapshot = ((lambda: backup.pre_migration_backup(app.state.engine))
                    if engine is None else None)
    migrate.run(app.state.engine, before_change=pre_snapshot)
    seeds.run(app.state.engine)

    EXEMPT_EXACT = {"/login", "/login/2fa", "/manifest.json", "/sw.js", "/health", "/branding/logo"}
    EXEMPT_PREFIX = ("/static",)

    @app.middleware("http")
    async def require_auth(request, call_next):
        path = request.url.path
        exempt = path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIX)
        request.state.user = None
        # Every path (incl. exempt/login/static, which also render base.html)
        # gets a theme so `data-theme` and the theme-aware loader always
        # resolve. Authenticated requests below override with the saved theme.
        request.state.active_theme = DEFAULT_THEME
        active_theme_var.set(DEFAULT_THEME)
        if not exempt:
            uid = request.session.get("user_id")
            # Missing "epoch" (cookie issued before this field existed) is
            # treated as epoch 0 — the default for every pre-existing user —
            # so already-logged-in clients aren't kicked out by this change.
            epoch = request.session.get("epoch", 0)
            if uid is not None:
                from sqlalchemy.orm import Session as _S
                with _S(app.state.engine) as s:
                    user = s.get(models.User, uid)
                    if user is not None and user.active and epoch == user.session_epoch:
                        s.expunge(user)
                        request.state.user = user
                        # Reuse this session to read the active theme once the
                        # user is confirmed; validate against the allowed set,
                        # falling back to the default on anything unexpected.
                        theme = models.get_setting(s, "active_theme", DEFAULT_THEME)
                        if theme not in ALLOWED_THEMES:
                            theme = DEFAULT_THEME
                        request.state.active_theme = theme
                        active_theme_var.set(theme)
            if request.state.user is None:
                request.session.clear()
                return RedirectResponse("/login", status_code=303)
            request.session["user_id"] = request.state.user.id  # rolling cookie
            request.session["epoch"] = request.state.user.session_epoch
        return await call_next(request)

    # CSRF defense-in-depth: reject state-changing requests whose Origin (or,
    # if absent, Referer) is cross-site. Modern browsers send Origin on every
    # unsafe-method request INCLUDING same-origin ones, so a genuine same-site
    # POST carries Origin == host and passes, while a cross-site forged POST is
    # blocked outright. When BOTH headers are absent (non-browser clients, the
    # test suite) we fall through to the always-on SameSite=Lax cookie, which
    # already blocks cross-site form POSTs — so this only ADDS protection and
    # never breaks a legitimate flow.
    from urllib.parse import urlparse as _urlparse

    @app.middleware("http")
    async def csrf_origin_guard(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            # Compare HOSTNAMES only (ignore scheme/port), and accept the request
            # if the source matches EITHER the Host header or X-Forwarded-Host —
            # so a reverse proxy that rewrites Host can't 403 a legitimate form
            # post. Absent Origin/Referer (non-browser clients) falls through to
            # the always-on SameSite=Lax cookie. Fail-open by design: only a
            # confidently cross-origin request is blocked.
            expected = {h for h in (
                _urlparse(f"//{request.headers.get('host','')}").hostname,
                _urlparse(f"//{request.headers.get('x-forwarded-host','')}").hostname,
            ) if h}
            source = None
            if request.headers.get("origin", "").strip() not in ("", "null"):
                source = _urlparse(request.headers["origin"]).hostname
            elif request.headers.get("referer"):
                source = _urlparse(request.headers["referer"]).hostname
            if source and expected and source not in expected:
                return Response("cross-origin request blocked", status_code=403)
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY,
                        max_age=60 * 60 * 24 * 90, https_only=config.COOKIE_SECURE)

    # Registered here -- after SessionMiddleware, before security_headers -- so
    # it lands just INSIDE the outermost layer. That position sees every
    # exception raised below it (routers, require_auth's own DB lookup, the
    # csrf guard, session decoding) and times the whole request, while leaving
    # the ordering rules above and below it exactly as they were: it neither
    # reads the session nor short-circuits, so nothing depends on where it sits
    # relative to the auth/csrf pair. It observes only -- no response is
    # touched and the exception is re-raised, so responses are unchanged.
    app.middleware("http")(observability.request_logging_middleware())

    # Outermost middleware (registered last -- Starlette's add_middleware/
    # @app.middleware both prepend, so whatever is added last wraps
    # everything else) so these headers land on EVERY response this app
    # produces: static files, 404s, the require_auth redirect-to-/login
    # short-circuit, /login itself, all of it -- not just templated pages.
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://maps.geoapify.com"
        )
        # HTML must always revalidate so a home-screen PWA never renders a stale
        # shell (the SW is network-first for navigations, but iOS's HTTP cache
        # can otherwise satisfy the fetch from Last-Modified heuristics).
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    from fastapi.responses import RedirectResponse as _RR
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request, exc):
        if exc.status_code == 401:
            return _RR("/login", status_code=303)
        if exc.status_code == 403:
            return templates.TemplateResponse(request, "403.html", {}, status_code=403)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/health")
    def health():
        # Touch the database so the healthcheck fails when SQLite is locked,
        # corrupt, or the /data volume didn't mount — otherwise the container
        # reports "healthy" while every real request 500s.
        from sqlalchemy import text as _text
        try:
            with app.state.engine.connect() as conn:
                conn.execute(_text("SELECT 1"))
        except Exception:
            return Response('{"ok": false}', status_code=503,
                            media_type="application/json")
        return {"ok": True}

    @app.get("/manifest.json", include_in_schema=False)
    def manifest():
        return FileResponse("app/static/manifest.json", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        # Inject APP_VERSION into the cache names so sw.js's bytes change every
        # release -> the browser detects a new worker -> install/activate purges
        # old caches. Served no-cache so the version check isn't itself cached.
        with open("app/static/sw.js") as fh:
            js = fh.read().replace("__APP_VERSION__", config.APP_VERSION)
        return Response(js, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    from app.routers import auth as auth_router
    app.include_router(auth_router.router)

    from app.routers import csv_io as csv_io_router
    app.include_router(csv_io_router.router)

    from app.routers import customers as customers_router
    app.include_router(customers_router.router)

    from app.routers import pools as pools_router
    app.include_router(pools_router.router)

    from app.routers import photos as photos_router
    app.include_router(photos_router.router)

    from app.routers import today as today_router
    app.include_router(today_router.router)

    from app.routers import visits as visits_router
    app.include_router(visits_router.router)

    from app.routers import week as week_router
    app.include_router(week_router.router)

    from app.routers import jobs as jobs_router
    app.include_router(jobs_router.router)

    from app.routers import quotes as quotes_router
    app.include_router(quotes_router.router)

    from app.routers import money as money_router
    app.include_router(money_router.router)

    from app.routers import settings as settings_router
    app.include_router(settings_router.router)

    from app.routers import company as company_router
    app.include_router(company_router.router)
    app.include_router(company_router.serve_router)

    from app.routers import console as console_router
    app.include_router(console_router.router)

    from app.routers import users as users_router
    app.include_router(users_router.router)

    from app.routers import account as account_router
    app.include_router(account_router.router)

    from app.routers import audit as audit_router
    app.include_router(audit_router.router)

    from app.routers import analytics as analytics_router
    app.include_router(analytics_router.router)

    from app.routers import inventory as inventory_router
    app.include_router(inventory_router.router)

    from app.routers import invoices as invoices_router
    app.include_router(invoices_router.router)

    from app.routers import routes as routes_router
    app.include_router(routes_router.router)

    from app.routers import contracts as contracts_router
    app.include_router(contracts_router.router)

    from app.routers import contract_template as contract_template_router
    app.include_router(contract_template_router.router)

    if config.RUN_SCHEDULER:
        from app.backup import backup_if_stale, start_scheduler
        from app.maintenance import run_nightly
        from app import mailer
        # Every job goes through logged_job: they run unattended at 1am with
        # nobody watching, and an unwrapped failure leaves nothing behind but a
        # line in a logger no one reads.
        app.state.scheduler = start_scheduler(app.state.engine)
        # start_scheduler owns the SCHEDULE of the jobs it registers (the
        # nightly backup), so re-point their funcs instead of re-registering
        # them here -- duplicating "cron 1:00" in two modules is how the two
        # drift apart. Wrapping whatever it registered also means a job added
        # there later is logged without anyone remembering to come back here.
        for job in app.state.scheduler.get_jobs():
            job.modify(func=observability.logged_job(job.func, job.name))
        app.state.scheduler.add_job(observability.logged_job(run_nightly),
                                    "cron", hour=1, minute=15,
                                    args=[app.state.engine])
        app.state.scheduler.add_job(observability.logged_job(mailer.flush_job),
                                    "interval", minutes=5,
                                    args=[app.state.engine])
        # The scheduler is in-process and non-persistent: a box that was down at
        # 01:00 (night deploy, Unraid reboot) silently skips that night and
        # nothing notices, so "14 daily generations" can quietly become 14
        # copies of one stale day. Never raises — a failure lands in
        # last_backup_error rather than stopping the app from starting.
        backup_if_stale(app.state.engine)

        @app.on_event("shutdown")
        def _stop_scheduler():
            # Stop APScheduler on SIGTERM (docker restart/redeploy) so a running
            # backup or mailer flush finishes instead of being killed mid-write.
            sched = getattr(app.state, "scheduler", None)
            if sched is not None:
                sched.shutdown(wait=True)

    return app

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config, models, observability, seeds
from app.db import Base, make_engine

# Re-exported on purpose: tests and older call sites import these from
# app.main, and the require_auth middleware below resolves ALLOWED_THEMES via
# this module's namespace (which keeps monkeypatch.setattr(app.main, ...)
# working). New code should import from app.templating directly.
from app.templating import ALLOWED_THEMES, DEFAULT_THEME, ThemeAwareLoader, active_theme_var, templates  # noqa: F401

log = logging.getLogger("openpoolpro.app")

# Routers import app.templating (not app.main), so they can be imported at
# module level again — create_app no longer needs 23 deferred imports to dodge
# a cycle. ROUTERS preserves the original registration order exactly (route
# matching order is load-bearing for overlapping path patterns).
from app.routers import (  # noqa: E402
    account,
    analytics,
    audit,
    auth,
    company,
    console,
    contract_template,
    contracts,
    csv_io,
    customers,
    inventory,
    invoices,
    jobs,
    money,
    photos,
    pools,
    quotes,
    routes,
    settings,
    sign,
    today,
    users,
    visits,
    week,
)

ROUTERS = (
    auth.router, csv_io.router, customers.router, pools.router, photos.router,
    today.router, visits.router, week.router, jobs.router, quotes.router,
    money.router, settings.router, company.router, company.serve_router,
    console.router, users.router, account.router, audit.router,
    analytics.router, inventory.router, invoices.router, routes.router,
    contracts.router, contract_template.router, sign.router,
)



def _acquire_single_instance_lock(app) -> None:
    """Refuse to run two app processes against one data dir.

    Correctness quietly assumes exactly ONE process: invoice numbering scans
    for the max, the login rate limiter and 2FA one-time-nonce store are
    in-process (N workers = N attempt budgets and replayable nonces — holes
    the security audit specifically closed), and the APScheduler jobs would
    duplicate. The Dockerfile runs a single uvicorn process, but nothing
    stopped an operator adding --workers 4 or a second replica on the same
    volume. An advisory flock, held for the process lifetime and released by
    the OS on ANY exit (crash included), makes the invariant explicit."""
    import fcntl
    os.makedirs(config.DATA_DIR, exist_ok=True)
    lock = open(os.path.join(config.DATA_DIR, ".instance.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError(
            "Another OpenPoolPro process already holds this DATA_DIR. Run exactly "
            "one (no `uvicorn --workers N`, no second container on this volume): "
            "rate limiting, 2FA replay protection, invoice numbering, and the "
            "scheduler are all single-process by design.") from None
    app.state.instance_lock = lock  # keep the fd alive for the process lifetime


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
        _acquire_single_instance_lock(app)
    app.state.engine = engine or make_engine()
    # Rate limiter for sensitive already-authenticated account actions (password
    # change, 2FA disable/regenerate) — a hijacked session shouldn't get
    # unlimited guesses at the current password or a 6-digit TOTP. App-scoped
    # (not a module singleton) so it never accumulates across the test suite.
    from app import security as _security
    app.state.account_limiter = _security.RateLimiter(max_fails=8, lock_seconds=900)
    # Throttle for the public /sign routes, keyed by client IP: a 256-bit
    # token can't be guessed, but scanners probing invalid links shouldn't
    # get unlimited free tries either. Generous cap — a customer fumbling a
    # pasted link must never lock themselves out.
    app.state.sign_limiter = _security.RateLimiter(max_fails=20, lock_seconds=900)
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
    # /sign is the customer-facing contract e-sign flow: its routes are gated
    # by a 256-bit single-use token in the URL, not by a session.
    EXEMPT_PREFIX = ("/static", "/sign")

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
        # Pin clients to HTTPS whenever the deployment is HTTPS (COOKIE_SECURE
        # is the same switch): without HSTS, one http:// bookmark is a
        # cleartext round trip before the redirect. 180 days, no
        # includeSubDomains — a self-hoster's domain may carry unrelated
        # plain-HTTP services this app must not break.
        if config.COOKIE_SECURE:
            response.headers["Strict-Transport-Security"] = "max-age=15552000"
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

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import RedirectResponse as _RR
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request, exc):
        # A browser form post that trips FastAPI validation (e.g. a required
        # Form(...) field left blank with client-side `required` stripped or
        # unsupported) used to get the default raw-JSON 422 — unreadable, and
        # it looked like the app ate everything typed. Browsers get a readable
        # page with a Go-back button (bfcache usually restores the form);
        # htmx and non-HTML callers keep the structured default.
        accept = request.headers.get("accept", "")
        if request.headers.get("hx-request") or "text/html" not in accept:
            from fastapi.exception_handlers import request_validation_exception_handler
            return await request_validation_exception_handler(request, exc)
        missing = sorted({str(e["loc"][-1]) for e in exc.errors()
                          if e.get("type") == "missing" and e.get("loc")})
        return templates.TemplateResponse(request, "422.html",
                                          {"missing": missing}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request, exc):
        if exc.status_code == 401:
            return _RR("/login", status_code=303)
        if exc.status_code == 403:
            return templates.TemplateResponse(request, "403.html", {}, status_code=403)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

    if not os.path.isfile("app/static/app.css"):
        log.error(
            "app/static/app.css is MISSING — every page will render unstyled. "
            "It is a build artifact (gitignored): run `python scripts/build_css.py` "
            "(the Docker image builds it automatically).")

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
        # Ops fields, still 200: a dead scheduler or stale backups shouldn't
        # restart-loop the container, but they were previously visible only as
        # a Settings-page banner an operator had to go looking for — an uptime
        # monitor can alert on these instead.
        from app import backup as _backup
        out = {"ok": True}
        sched = getattr(app.state, "scheduler", None)
        if sched is not None:
            out["scheduler_running"] = bool(getattr(sched, "running", False))
        age = _backup.newest_backup_age_hours()
        if age is not None:
            out["backup_age_hours"] = round(age, 1)
            out["backup_fresh"] = age < 36
        return out

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

    for r in ROUTERS:
        app.include_router(r)

    if config.RUN_SCHEDULER:
        from app import mailer
        from app.backup import backup_if_stale, start_scheduler
        from app.maintenance import run_nightly
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

import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, models, seeds, timefmt  # noqa: F401
from app.db import Base, make_engine

templates = Jinja2Templates(directory="app/templates")
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


def create_app(engine=None) -> FastAPI:
    app = FastAPI(title="Coastline Pools Planner")
    app.state.engine = engine or make_engine()
    Base.metadata.create_all(app.state.engine)
    from app import migrate
    migrate.run(app.state.engine)
    seeds.run(app.state.engine)

    EXEMPT_EXACT = {"/login", "/login/2fa", "/manifest.json", "/sw.js", "/health", "/branding/logo"}
    EXEMPT_PREFIX = ("/static",)

    @app.middleware("http")
    async def require_auth(request, call_next):
        path = request.url.path
        exempt = path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIX)
        request.state.user = None
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
            if request.state.user is None:
                request.session.clear()
                return RedirectResponse("/login", status_code=303)
            request.session["user_id"] = request.state.user.id  # rolling cookie
            request.session["epoch"] = request.state.user.session_epoch
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY,
                        max_age=60 * 60 * 24 * 90, https_only=config.COOKIE_SECURE)

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
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
        )
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
        return {"ok": True}

    @app.get("/manifest.json", include_in_schema=False)
    def manifest():
        return FileResponse("app/static/manifest.json")

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return FileResponse("app/static/sw.js", media_type="application/javascript")

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

    from app.routers import invoices as invoices_router
    app.include_router(invoices_router.router)

    if config.RUN_SCHEDULER:
        from app.backup import start_scheduler
        from app.maintenance import run_nightly
        app.state.scheduler = start_scheduler(app.state.engine)
        app.state.scheduler.add_job(run_nightly, "cron", hour=1, minute=15,
                                    args=[app.state.engine])

    return app

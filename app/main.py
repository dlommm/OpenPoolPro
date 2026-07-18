from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, models, seeds  # noqa: F401
from app.db import Base, make_engine

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["dollars"] = lambda cents: f"${(cents or 0) / 100:,.2f}"


def create_app(engine=None) -> FastAPI:
    app = FastAPI(title="Coastline Pools Planner")
    app.state.engine = engine or make_engine()
    Base.metadata.create_all(app.state.engine)
    seeds.run(app.state.engine)

    EXEMPT_EXACT = {"/login", "/manifest.json", "/sw.js", "/health"}
    EXEMPT_PREFIX = ("/static",)

    @app.middleware("http")
    async def require_auth(request, call_next):
        path = request.url.path
        exempt = path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIX)
        if not exempt and not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY,
                        max_age=60 * 60 * 24 * 90)

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

    from app.routers import customers as customers_router
    app.include_router(customers_router.router)

    from app.routers import pools as pools_router
    app.include_router(pools_router.router)

    from app.routers import photos as photos_router
    app.include_router(photos_router.router)

    from app.routers import today as today_router
    app.include_router(today_router.router)

    return app

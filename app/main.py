from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config, models, seeds  # noqa: F401
from app.db import Base, make_engine

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["dollars"] = lambda cents: f"${(cents or 0) / 100:,.2f}"


def create_app(engine=None) -> FastAPI:
    app = FastAPI(title="Coastline Pools Planner")
    app.state.engine = engine or make_engine()
    Base.metadata.create_all(app.state.engine)
    seeds.run(app.state.engine)

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

    return app

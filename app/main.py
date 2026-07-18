from fastapi import FastAPI

from app import config, seeds
from app.db import Base, make_engine
from app import models  # noqa: F401


def create_app(engine=None) -> FastAPI:
    app = FastAPI(title="Coastline Pools Planner")
    app.state.engine = engine or make_engine()
    Base.metadata.create_all(app.state.engine)
    seeds.run(app.state.engine)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = None  # uvicorn entrypoint set in Task 19 via factory

import os

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    if url is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        url = f"sqlite:///{os.path.join(config.DATA_DIR, 'app.db')}"
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 5}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session

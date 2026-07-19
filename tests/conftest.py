import pytest
from fastapi.testclient import TestClient

import app.config as config


@pytest.fixture
def engine():
    from app.db import make_engine
    return make_engine("sqlite://")


@pytest.fixture
def client(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "APP_PASSWORD", "testpass")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from app.main import create_app
    app = create_app(engine=engine)
    c = TestClient(app)
    c.post("/login", data={"username": "owner", "password": "testpass"})
    return c


@pytest.fixture
def tech_client(engine, client):
    """Factory: tech_client(name='sam', **flags) -> logged-in TestClient for that tech."""
    from sqlalchemy.orm import Session
    from app import models
    from app.auth_core import hash_password
    from app.main import create_app

    def make(name="sam", password="techpass", **flags):
        with Session(engine) as s:
            s.add(models.User(name=name.title(), username=name.strip().lower(), role="tech", active=True,
                              password_hash=hash_password(password), **flags))
            s.commit()
        c = TestClient(create_app(engine=engine))
        r = c.post("/login", data={"username": name.strip().lower(), "password": password},
                   follow_redirects=False)
        assert r.status_code == 303, f"tech_client login failed for {name!r}"
        return c

    return make

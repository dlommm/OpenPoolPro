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
    c.post("/login", data={"password": "testpass"})
    return c

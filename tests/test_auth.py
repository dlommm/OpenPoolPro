from fastapi.testclient import TestClient

import app.config as config
from app.main import create_app


def make_client(engine, monkeypatch, password="testpass"):
    monkeypatch.setattr(config, "APP_PASSWORD", password)
    return TestClient(create_app(engine=engine))


def test_redirects_when_logged_out(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_wrong_password(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.post("/login", data={"password": "nope"})
    assert "Wrong password" in r.text


def test_login_logout_flow(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.post("/login", data={"password": "testpass"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/health").status_code == 200
    r = c.get("/", follow_redirects=False)
    assert r.status_code != 303 or r.headers.get("location") != "/login"
    c.post("/logout")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303

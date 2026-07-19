from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.config as config
from app.main import create_app


def make_client(engine, monkeypatch, password="testpass"):
    monkeypatch.setattr(config, "APP_PASSWORD", password)
    return TestClient(create_app(engine=engine))


def login(c, username="owner", password="testpass"):
    return c.post("/login", data={"username": username, "password": password},
                  follow_redirects=False)


def test_redirects_when_logged_out(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_same_error_for_bad_user_and_bad_password(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r1 = c.post("/login", data={"username": "ghost", "password": "x"})
    r2 = c.post("/login", data={"username": "owner", "password": "x"})
    assert "Wrong username or password" in r1.text
    assert "Wrong username or password" in r2.text


def test_login_logout_flow(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    c.post("/logout")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_username_case_insensitive(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    assert login(c, username="OwNeR").status_code == 303


def test_disabled_user_cut_off(engine, monkeypatch):
    from sqlalchemy import select
    from app import models
    c = make_client(engine, monkeypatch)
    login(c)
    with Session(engine) as s:
        s.scalars(select(models.User).where(models.User.username == "owner")).one().active = False
        s.commit()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_non_ascii_password_no_500(engine, monkeypatch):
    c = make_client(engine, monkeypatch, password="café-pass")
    r = c.post("/login", data={"username": "owner", "password": "café-wrong"})
    assert r.status_code == 200 and "Wrong username or password" in r.text
    assert login(c, password="café-pass").status_code == 303


def test_prefixed_lookalike_paths_not_exempt(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    for path in ["/healthz", "/loginfoo", "/sw.js.map"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_exempt_paths_reachable_logged_out(engine, monkeypatch):
    c = make_client(engine, monkeypatch)
    for path in ["/login", "/manifest.json", "/sw.js", "/health"]:
        assert c.get(path, follow_redirects=False).status_code == 200

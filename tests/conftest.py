import pytest
from fastapi.testclient import TestClient

import app.config as config
from app import auth_core

# Bcrypt at production cost (~250 ms/hash) dominated this suite's runtime:
# every `client` fixture rebuilds the app (seeds.run hashes APP_PASSWORD) and
# then logs in (a verify), so 500+ tests each paid ~0.5 s of pure KDF.
#
# Mutate the shared CryptContext IN PLACE rather than rebinding it: app/security.py
# does `from app.auth_core import pwd_context`, so a rebind would leave backup-code
# hashing on the old context. The scheme stays bcrypt — only the work factor moves,
# so every code path under test behaves identically.
auth_core.pwd_context.update(bcrypt__rounds=4)

# The TestClient speaks plain http://testserver, and httpx's cookie jar rightly
# refuses to store Secure cookies from an insecure origin — under the
# production default (COOKIE_SECURE=1) every login in the suite would silently
# fail. Tests that assert Secure-flag behavior monkeypatch this back to True.
config.COOKIE_SECURE = False

_CLIENT_FIXTURES = {"client", "tech_client"}


def pytest_collection_modifyitems(items):
    """Auto-mark every test that never builds the app as `unit`.

    Derived from the fixtures a test actually requests rather than a hand-kept
    file list, so a new pure-logic test joins the fast subset for free and one
    that grows an HTTP client drops out of it automatically.
    """
    for item in items:
        if not _CLIENT_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.unit)


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

from tests.conftest import make_owner


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_get_session_dependency_injects(engine, monkeypatch, tmp_path):
    import app.config as config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    make_owner(engine, password="probe-pass")
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.db import get_session
    from app.main import create_app

    app = create_app(engine=engine)

    @app.get("/session-probe")
    def probe(session: Session = Depends(get_session)):
        return {"ok": session.is_active}

    client = TestClient(app)
    client.post("/login", data={"username": "owner", "password": "probe-pass"})
    r = client.get("/session-probe")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

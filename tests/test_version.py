import app.config as config


def test_settings_shows_default_version(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "OpenPoolPro" in r.text
    assert config.APP_VERSION in r.text  # default "dev"


def test_settings_shows_baked_version(client, monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "v1.0.0")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "OpenPoolPro v1.0.0" in r.text

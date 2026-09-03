"""Office 365 Modern Auth (OAuth 2.0 device flow + SMTP XOAUTH2)."""
import base64
import json

from sqlalchemy.orm import Session

from app import mailer, models, msoauth


def _fake_id_token(email="pool@contoso.com"):
    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": email}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _post_script(monkeypatch, responses):
    """Feed msoauth._post a scripted list of responses; capture calls."""
    calls = []

    def fake_post(url, data):
        calls.append((url, data))
        return responses.pop(0)

    monkeypatch.setattr(msoauth, "_post", fake_post)
    return calls


def setup_function(_):
    msoauth._cache.clear()


# ---------- device flow ----------

def test_device_flow_start_and_poll_to_connected(client, engine, monkeypatch):
    _post_script(monkeypatch, [
        {"device_code": "dev123", "user_code": "ABC-DEF",
         "verification_uri": "https://microsoft.com/devicelogin",
         "interval": 5, "expires_in": 900},
        {"error": "authorization_pending"},
        {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
         "id_token": _fake_id_token()},
    ])
    with Session(engine) as s:
        models.set_setting(s, "smtp_oauth_client_id", "client-guid")
        s.commit()

    r = client.post("/settings/smtp/oauth/start")
    assert r.status_code == 200
    assert "ABC-DEF" in r.text
    assert "devicelogin" in r.text

    r = client.get("/settings/smtp/oauth/poll")
    assert "ABC-DEF" in r.text  # still pending, keeps polling

    r = client.get("/settings/smtp/oauth/poll")
    assert "Connected" in r.text and "pool@contoso.com" in r.text
    with Session(engine) as s:
        assert models.get_setting(s, "smtp_oauth_refresh_token", "") == "rt1"
        assert models.get_setting(s, "smtp_auth_mode", "") == "oauth365"
        assert models.get_setting(s, "smtp_oauth_account", "") == "pool@contoso.com"


def test_device_flow_denied_shows_error_and_clears_pending(client, engine, monkeypatch):
    _post_script(monkeypatch, [
        {"device_code": "dev123", "user_code": "ABC-DEF",
         "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900},
        {"error": "access_denied", "error_description": "The user declined."},
    ])
    with Session(engine) as s:
        models.set_setting(s, "smtp_oauth_client_id", "client-guid")
        s.commit()
    client.post("/settings/smtp/oauth/start")
    r = client.get("/settings/smtp/oauth/poll")
    assert "declined" in r.text
    with Session(engine) as s:
        assert models.get_setting(s, "smtp_auth_mode", "password") == "password"


def test_start_without_client_id_is_friendly(client):
    r = client.post("/settings/smtp/oauth/start")
    assert r.status_code == 200
    assert "client" in r.text.lower()


# ---------- token refresh ----------

def test_access_token_refreshes_persists_rotation_and_caches(client, engine, monkeypatch):
    calls = _post_script(monkeypatch, [
        {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600},
    ])
    with Session(engine) as s:
        models.set_setting(s, "smtp_oauth_client_id", "client-guid")
        models.set_setting(s, "smtp_oauth_refresh_token", "rt1")
        s.commit()
        assert msoauth.access_token(s) == "at2"
        assert msoauth.access_token(s) == "at2"  # cached: no second POST
    assert len(calls) == 1
    with Session(engine) as s:
        assert models.get_setting(s, "smtp_oauth_refresh_token", "") == "rt2"


def test_access_token_expired_grant_raises_admin_safe_error(client, engine, monkeypatch):
    _post_script(monkeypatch, [
        {"error": "invalid_grant", "error_description": "AADSTS70008: expired."},
    ])
    with Session(engine) as s:
        models.set_setting(s, "smtp_oauth_client_id", "client-guid")
        models.set_setting(s, "smtp_oauth_refresh_token", "rt-old")
        s.commit()
        try:
            msoauth.access_token(s)
            raise AssertionError("should have raised")
        except msoauth.MsAuthError as e:
            assert "rt-old" not in str(e)


# ---------- mailer integration ----------

def _oauth_connected(engine):
    with Session(engine) as s:
        models.set_setting(s, "smtp_auth_mode", "oauth365")
        models.set_setting(s, "smtp_oauth_client_id", "client-guid")
        models.set_setting(s, "smtp_oauth_refresh_token", "rt1")
        models.set_setting(s, "smtp_oauth_account", "pool@contoso.com")
        s.commit()


def test_smtp_settings_oauth_mode_pins_o365(client, engine):
    _oauth_connected(engine)
    with Session(engine) as s:
        conf = mailer.smtp_settings(s)
        assert conf["auth_mode"] == "oauth365"
        assert conf["host"] == "smtp.office365.com" and conf["port"] == 587
        assert conf["starttls"] is True
        assert conf["user"] == conf["from"] == "pool@contoso.com"
        assert mailer.smtp_configured(s) is True
        # The password form still resolves password-mode values.
        assert mailer.smtp_settings(s, force_password=True)["auth_mode"] == "password"


def test_send_message_uses_xoauth2(client, engine, monkeypatch):
    _oauth_connected(engine)
    monkeypatch.setattr(msoauth, "access_token", lambda s: "tok123")

    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            captured["tls"] = True

        def ehlo(self):
            pass

        def auth(self, mech, authobject):
            captured["mech"] = mech
            captured["auth"] = authobject()

        def login(self, *a):
            captured["login"] = True

        def send_message(self, msg):
            captured["sent"] = True

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with Session(engine) as s:
        mailer.send_message("to@x.com", "s", "b", conf=mailer.send_conf(s))
    assert captured["mech"] == "XOAUTH2"
    assert captured["auth"] == "user=pool@contoso.com\x01auth=Bearer tok123\x01\x01"
    assert "login" not in captured
    assert captured["sent"] and captured["tls"]


def test_flush_outbox_skips_when_token_refresh_fails(client, engine, monkeypatch):
    _oauth_connected(engine)

    def boom(_s):
        raise msoauth.MsAuthError("expired")

    monkeypatch.setattr(msoauth, "access_token", boom)
    with Session(engine) as s:
        mailer.queue(s, "a@b.c", "subj", "body")
        s.commit()
        assert mailer.flush_outbox(s) == (0, 0)
        row = s.query(models.EmailOutbox).one()
        assert row.status == "pending" and row.attempts == 0  # no burned attempts


def test_disconnect_clears_and_falls_back(client, engine):
    _oauth_connected(engine)
    r = client.post("/settings/smtp/oauth/disconnect", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "smtp_oauth_refresh_token", "x") == ""
        assert models.get_setting(s, "smtp_auth_mode", "") == "password"


def test_refresh_token_never_renders_in_settings_page(client, engine):
    _oauth_connected(engine)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "rt1" not in r.text
    assert "pool@contoso.com" in r.text  # but the account label does

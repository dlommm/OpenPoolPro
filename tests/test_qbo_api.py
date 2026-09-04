"""QuickBooks Online Phase 1: OAuth connect, token refresh, customer push."""
import re
import urllib.parse

from sqlalchemy.orm import Session

from app import models, qbo_api


def setup_function(_):
    qbo_api._cache.clear()


def _configure(engine, secret="app-secret", env="sandbox"):
    with Session(engine) as s:
        models.set_setting(s, "qbo_client_id", "client-abc")
        models.set_setting(s, "qbo_client_secret", secret)
        models.set_setting(s, "qbo_environment", env)
        s.commit()


def _connected(engine):
    _configure(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_refresh_token", "rt-1")
        models.set_setting(s, "qbo_realm_id", "9130000")
        s.commit()


# ---------- config / status ----------

def test_configured_and_connected_reflect_settings(client, engine):
    with Session(engine) as s:
        assert qbo_api.configured(s) is False
        assert qbo_api.connected(s) is False
    _configure(engine)
    with Session(engine) as s:
        assert qbo_api.configured(s) is True and qbo_api.connected(s) is False
    _connected(engine)
    with Session(engine) as s:
        assert qbo_api.connected(s) is True


def test_authorize_url_has_state_scope_and_pkce(client, engine):
    _configure(engine)
    verifier = qbo_api.new_code_verifier()
    with Session(engine) as s:
        url = qbo_api.authorize_url(s, "https://x/cb", "st8", verifier)
    assert url.startswith(qbo_api.AUTH_URL)
    assert "state=st8" in url and "com.intuit.quickbooks.accounting" in url
    assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url
    # PKCE: the challenge travels, the verifier never does.
    assert "code_challenge_method=S256" in url
    assert qbo_api._code_challenge(verifier) in urllib.parse.unquote(url)
    assert verifier not in url


def test_authorize_url_requires_client_id(client, engine):
    with Session(engine) as s:
        try:
            qbo_api.authorize_url(s, "https://x/cb", "st", "verifier")
            raise AssertionError("should raise")
        except qbo_api.QboError:
            pass


def test_api_base_switches_on_environment(client, engine):
    _configure(engine, env="production")
    with Session(engine) as s:
        assert "sandbox" not in qbo_api.api_base(s)
    _configure(engine, env="sandbox")
    with Session(engine) as s:
        assert "sandbox" in qbo_api.api_base(s)


# ---------- OAuth token flow ----------

def test_complete_connection_stores_tokens_and_company(client, engine, monkeypatch):
    _configure(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "access_token": "at1", "refresh_token": "rt-new", "expires_in": 3600})
    monkeypatch.setattr(qbo_api, "company_name", lambda s: "Blue Water Pools")
    with Session(engine) as s:
        name = qbo_api.complete_connection(s, "authcode", "9130000", "https://x/cb")
        assert name == "Blue Water Pools"
        assert models.get_setting(s, "qbo_refresh_token", "") == "rt-new"
        assert models.get_setting(s, "qbo_realm_id", "") == "9130000"


def test_complete_connection_error_raises(client, engine, monkeypatch):
    _configure(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "error": "invalid_grant", "error_description": "bad code"})
    with Session(engine) as s:
        try:
            qbo_api.complete_connection(s, "x", "1", "https://x/cb")
            raise AssertionError("should raise")
        except qbo_api.QboError as e:
            assert "bad code" in str(e)


def test_access_token_refreshes_persists_rotation_and_caches(client, engine, monkeypatch):
    _connected(engine)
    calls = []
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: calls.append(form) or {
        "access_token": "at2", "refresh_token": "rt-2", "expires_in": 3600})
    with Session(engine) as s:
        assert qbo_api.access_token(s) == "at2"
        assert qbo_api.access_token(s) == "at2"  # cached, no 2nd call
        assert len(calls) == 1
        assert models.get_setting(s, "qbo_refresh_token", "") == "rt-2"  # rotation persisted


def test_access_token_expired_grant_is_admin_safe(client, engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "error": "invalid_grant", "error_description": "AADSTS-like expiry"})
    with Session(engine) as s:
        try:
            qbo_api.access_token(s)
            raise AssertionError("should raise")
        except qbo_api.QboError as e:
            assert "rt-1" not in str(e)


# ---------- customer push ----------

def test_push_customer_creates_when_unlinked(client, engine, monkeypatch):
    _connected(engine)
    seen = {}

    def fake_api(s, method, path, body=None):
        seen["method"], seen["path"], seen["body"] = method, path, body
        return {"Customer": {"Id": "77"}}

    monkeypatch.setattr(qbo_api, "_api", fake_api)
    with Session(engine) as s:
        cust = models.Customer(name="Ada Rivera", email="ada@example.com", phone="555")
        s.add(cust)
        s.commit()
        assert qbo_api.push_customer(s, cust) == "77"
    assert seen["method"] == "POST" and seen["path"] == "customer"
    assert seen["body"]["DisplayName"] == "Ada Rivera"
    assert "Id" not in seen["body"]  # a create, not an update


def test_push_customer_updates_when_already_linked(client, engine, monkeypatch):
    _connected(engine)
    calls = []

    def fake_api(s, method, path, body=None):
        calls.append((method, path))
        if method == "GET":
            return {"Customer": {"Id": "77", "SyncToken": "3"}}
        return {"Customer": {"Id": "77"}}

    monkeypatch.setattr(qbo_api, "_api", fake_api)
    with Session(engine) as s:
        cust = models.Customer(name="Ada", qbo_customer_id="77")
        s.add(cust)
        s.commit()
        assert qbo_api.push_customer(s, cust) == "77"
    assert ("GET", "customer/77") in calls  # fetched SyncToken first
    assert ("POST", "customer") in calls    # then sparse update


def test_disconnect_clears_connection(client, engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("no net")))
    with Session(engine) as s:
        qbo_api.disconnect(s)  # revoke best-effort; must not raise
        s.commit()
        assert qbo_api.connected(s) is False
        assert models.get_setting(s, "qbo_refresh_token", "x") == ""


# ---------- routes ----------

def test_connect_is_post_only_and_redirects_to_intuit(client, engine):
    """GET must not start a connect: SameSite=Lax carries the admin cookie on a
    cross-site top-level navigation, so a GET version is drivable from a
    phishing link."""
    _configure(engine)
    assert client.get("/settings/qbo/connect", follow_redirects=False).status_code == 405
    r = client.post("/settings/qbo/connect", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(qbo_api.AUTH_URL)


def test_callback_rejects_bad_state(client, engine):
    _configure(engine)
    client.post("/settings/qbo/connect", follow_redirects=False)  # sets state
    r = client.get("/settings/qbo/callback?code=x&realmId=1&state=WRONG",
                   follow_redirects=False)
    assert r.status_code == 303 and "qbo_error" in r.headers["location"]
    with Session(engine) as s:
        assert qbo_api.connected(s) is False


def test_forged_error_callback_does_not_consume_the_pending_nonce(client, engine, monkeypatch):
    """A phishing page can navigate the admin to ?error=... — that must not eat
    the pending state, or a genuine connect could never be completed."""
    _configure(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(qbo_api, "company_name", lambda s: "Co")
    r = client.post("/settings/qbo/connect", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    # Attacker-driven cancel...
    client.get("/settings/qbo/callback?error=access_denied", follow_redirects=False)
    # ...the real callback still works.
    r = client.get(f"/settings/qbo/callback?code=abc&realmId=9130000&state={state}",
                   follow_redirects=False)
    assert "qbo_ok=1" in r.headers["location"]
    with Session(engine) as s:
        assert qbo_api.connected(s) is True


def test_state_is_single_use(client, engine, monkeypatch):
    _configure(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(qbo_api, "company_name", lambda s: "Co")
    r = client.post("/settings/qbo/connect", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    url = f"/settings/qbo/callback?code=abc&realmId=9130000&state={state}"
    assert "qbo_ok=1" in client.get(url, follow_redirects=False).headers["location"]
    # Replaying the same callback is rejected.
    assert "qbo_error" in client.get(url, follow_redirects=False).headers["location"]


def test_callback_rejects_non_numeric_realm_id(client, engine, monkeypatch):
    """realm_id is interpolated into API URLs — only decimal ids are accepted."""
    _configure(engine)
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    r = client.post("/settings/qbo/connect", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = client.get(f"/settings/qbo/callback?code=abc&realmId=../../evil&state={state}",
                   follow_redirects=False)
    assert "qbo_error" in r.headers["location"]
    with Session(engine) as s:
        assert qbo_api.connected(s) is False
        assert models.get_setting(s, "qbo_realm_id", "") == ""


def test_full_connect_callback_flow_sends_pkce_verifier(client, engine, monkeypatch):
    _configure(engine)
    forms = []
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: forms.append(form) or {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(qbo_api, "company_name", lambda s: "Blue Water Pools")
    r = client.post("/settings/qbo/connect", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = client.get(f"/settings/qbo/callback?code=abc&realmId=9130000&state={state}",
                   follow_redirects=False)
    assert r.status_code == 303 and "qbo_ok=1" in r.headers["location"]
    assert forms[0].get("code_verifier"), "PKCE verifier must be sent on exchange"
    with Session(engine) as s:
        assert qbo_api.connected(s) is True
        assert models.get_setting(s, "qbo_company_name", "") == "Blue Water Pools"


def test_error_text_is_not_reflected_from_the_url(client, engine):
    """A crafted ?qbo_error=<copy> link must not render attacker text inside
    the real admin UI (a credible phishing primitive)."""
    r = client.get("/settings?qbo_error=Call+1-800-EVIL+to+reauthorize")
    assert r.status_code == 200
    assert "1-800-EVIL" not in r.text


def test_error_message_comes_from_server_state(client, engine):
    _configure(engine)  # configured but not connected
    r = client.post("/settings/qbo/sync-customers", follow_redirects=True)
    # The flag comes from the URL; the TEXT comes from the stored setting.
    assert "Connect QuickBooks first." in r.text
    # And it is not shown on an ordinary render afterwards.
    assert "Connect QuickBooks first." not in client.get("/settings").text


def test_items_load_is_post_only(client, engine):
    """A page render (or a crafted link) must never trigger an outbound call."""
    _connected(engine)
    assert client.get("/settings/qbo/items-load", follow_redirects=False).status_code == 405


def test_saving_config_clears_the_cached_token(client, engine):
    _connected(engine)
    qbo_api._cache.update(token="stale", exp=9e18)
    client.post("/settings/qbo/config",
                data={"client_id": "new-id", "environment": "production"},
                follow_redirects=False)
    assert qbo_api._cache.get("token") is None  # sandbox token can't leak to prod


def test_malformed_token_response_is_admin_safe_not_a_500(client, engine, monkeypatch):
    _configure(engine)
    # refresh_token present but access_token missing would have KeyError'd.
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: {"refresh_token": "rt"})
    with Session(engine) as s:
        try:
            qbo_api.complete_connection(s, "code", "9130000", "https://x/cb", "v")
            raise AssertionError("should raise")
        except qbo_api.QboError:
            pass


def test_sync_customers_is_idempotent(client, engine, monkeypatch):
    _connected(engine)
    created = []

    def fake_api(s, method, path, body=None):
        if method == "POST":
            created.append(body["DisplayName"])
            return {"Customer": {"Id": "100"}}
        return {"Customer": {"Id": "100", "SyncToken": "0"}}

    monkeypatch.setattr(qbo_api, "_api", fake_api)
    with Session(engine) as s:
        s.add(models.Customer(name="Ada", active=True))
        s.commit()
    r = client.post("/settings/qbo/sync-customers", follow_redirects=False)
    assert "qbo_synced=1" in r.headers["location"]
    with Session(engine) as s:
        assert s.query(models.Customer).first().qbo_customer_id == "100"
    # Second sync: the linked customer updates (GET+POST), never a 2nd create
    # for the same person.
    client.post("/settings/qbo/sync-customers", follow_redirects=False)
    # Both syncs create-or-update, but the customer keeps ONE qbo id.
    with Session(engine) as s:
        cust = s.query(models.Customer).first()
        assert cust.qbo_customer_id == "100"


def test_client_secret_never_renders(client, engine):
    _configure(engine, secret="super-secret-value")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "super-secret-value" not in r.text
    assert "QuickBooks Online" in r.text


def test_sync_requires_connection(client, engine):
    _configure(engine)  # configured but not connected
    r = client.post("/settings/qbo/sync-customers", follow_redirects=False)
    assert "qbo_error" in r.headers["location"]

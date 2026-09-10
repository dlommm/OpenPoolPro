"""QuickBooks Online Phase 1: OAuth connect, token refresh, customer push."""
import re
import urllib.parse

from sqlalchemy.orm import Session

from app import models, qbo_api


def setup_function(_):
    qbo_api._cache.clear()


def _configure(engine, secret="app-secret", env="sandbox"):
    models.Base.metadata.create_all(engine)
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

    def fake_api(s, method, path, body=None, **kwargs):
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

    def fake_api(s, method, path, body=None, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return {"Customer": {"Id": "77", "SyncToken": "3"}}
        return {"Customer": {"Id": "77"}}

    monkeypatch.setattr(qbo_api, "_api", fake_api)
    with Session(engine) as s:
        cust = models.Customer(name="Ada", qbo_customer_id="77", qbo_customer_scope="sandbox:9130000")
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

    def fake_api(s, method, path, body=None, **kwargs):
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


def test_invoice_wire_format_and_pdf(engine, monkeypatch):
    import io
    import json
    _connected(engine)
    calls = []
    monkeypatch.setattr(qbo_api, "access_token", lambda s: "access")
    def respond(req, **kwargs):
        calls.append(req)
        return io.BytesIO(b"%PDF-1.7 data" if "/pdf?" in req.full_url else b'{"Invoice":{"Id":"42"}}')
    monkeypatch.setattr(qbo_api.urllib.request, "urlopen", respond)
    with Session(engine) as s:
        original = {"CustomerRef": {"value": "1"}, "EmailStatus": "NeedToSend"}
        assert qbo_api.create_invoice(s, original, "stable-1")["Id"] == "42"
        assert original["EmailStatus"] == "NeedToSend"
        qbo_api.send_invoice(s, "42", "a+b@example.com", "send-1")
        qbo_api.get_invoice(s, "42")
        assert qbo_api.invoice_pdf(s, "42").startswith(b"%PDF-")
    assert json.loads(calls[0].data)["EmailStatus"] == "NotSet"
    assert "requestid=stable-1" in calls[0].full_url
    assert "minorversion=75" in calls[0].full_url
    assert "/42/send?sendTo=a%2Bb%40example.com" in calls[1].full_url
    assert calls[1].get_header("Content-type") == "application/octet-stream"
    assert calls[1].data == b""
    assert "include=invoiceLink" in calls[2].full_url
    assert calls[3].get_header("Accept") == "application/pdf"


def test_send_timeout_is_ambiguous_and_never_retried(engine, monkeypatch):
    import pytest
    calls = []
    _connected(engine)
    monkeypatch.setattr(qbo_api, "access_token", lambda s: "access")
    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()
    monkeypatch.setattr(qbo_api.urllib.request, "urlopen", fail)
    with Session(engine) as s, pytest.raises(qbo_api.QboError) as err:
        qbo_api.send_invoice(s, "42")
    assert err.value.outcome_unknown and len(calls) == 1


def test_query_pagination_and_identifier_validation(engine, monkeypatch):
    import pytest
    calls = []
    def fake(s, method, path, **kw):
        calls.append(urllib.parse.unquote(path))
        return {"QueryResponse": {"Payment": [{"Id": "1"}, {"Id": "2"}] if len(calls) == 1 else [{"Id": "3"}]}}
    monkeypatch.setattr(qbo_api, "_api", fake)
    with Session(engine) as s:
        assert len(qbo_api.query_payments(s, page_size=2)) == 3
        with pytest.raises(qbo_api.QboError):
            qbo_api.query_entities(s, "Invoice where Id='1'")
    assert "startposition 1 maxresults 2" in calls[0]
    assert "startposition 3 maxresults 2" in calls[1]


def test_cache_is_scoped_to_company(engine, monkeypatch):
    _connected(engine)
    calls = []
    monkeypatch.setattr(qbo_api, "_token_request", lambda s, form: calls.append(1) or {"access_token": str(len(calls))})
    with Session(engine) as s:
        assert qbo_api.access_token(s) == "1"
        models.set_setting(s, "qbo_realm_id", "222")
        s.commit()
        assert qbo_api.access_token(s) == "2"


def test_customer_wrong_company_fails_before_network(engine, monkeypatch):
    import pytest
    _connected(engine)
    monkeypatch.setattr(qbo_api, "_api", lambda *a, **k: pytest.fail("cross-company call"))
    with Session(engine) as s:
        customer = models.Customer(name="Ada", qbo_customer_id="77", qbo_customer_scope="production:999")
        with pytest.raises(qbo_api.QboError, match="mapping"):
            qbo_api.push_customer(s, customer)


def test_environment_change_disconnects_and_invalidates_mapping(client, engine):
    _connected(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_item_service_id", "22")
        models.set_setting(s, "qbo_billing_enabled", "1")
        s.commit()
    client.post("/settings/qbo/config", data={"client_id": "client-abc", "environment": "production"})
    with Session(engine) as s:
        assert not qbo_api.connected(s)
        assert not models.get_setting(s, "qbo_item_service_id", "")
        assert not models.get_setting(s, "qbo_billing_enabled", "")


def test_http_408_mutation_unknown_401_clears_cache(engine, monkeypatch):
    import io
    import urllib.error

    import pytest
    _connected(engine)
    monkeypatch.setattr(qbo_api, "access_token", lambda s: "token")
    for status in (408, 401, 400, 503):
        def fail(*args, status=status, **kwargs):
            raise urllib.error.HTTPError("url", status, "failure", {}, io.BytesIO(b'{}'))
        monkeypatch.setattr(qbo_api.urllib.request, "urlopen", fail)
        qbo_api._cache["token"] = "stale"
        with Session(engine) as s, pytest.raises(qbo_api.QboError) as caught:
            qbo_api.send_invoice(s, "42")
        assert caught.value.outcome_unknown == (status in (408, 503))
        if status == 401:
            assert not qbo_api._cache


def test_malformed_success_does_not_mark_send_or_empty_sync(engine, monkeypatch):
    import io

    import pytest
    _connected(engine)
    monkeypatch.setattr(qbo_api, "access_token", lambda s: "token")
    for data in (b"", b"[]", b"null", b"oops"):
        monkeypatch.setattr(qbo_api.urllib.request, "urlopen", lambda *a, data=data, **k: io.BytesIO(data))
        with Session(engine) as s, pytest.raises(qbo_api.QboError) as caught:
            qbo_api.send_invoice(s, "42")
        assert caught.value.outcome_unknown
    monkeypatch.setattr(qbo_api, "_api", lambda *a, **k: {})
    with Session(engine) as s, pytest.raises(qbo_api.QboError):
        qbo_api.query_payments(s)


def test_customer_create_freezes_payload_after_unknown_response(engine, monkeypatch):
    import pytest
    _connected(engine)
    calls = []
    def fake(s, method, path, body=None, **kwargs):
        calls.append((dict(body), kwargs["request_id"]))
        if len(calls) == 1:
            raise qbo_api.QboError("unknown", outcome_unknown=True)
        return {"Customer": {"Id": "12"}}
    monkeypatch.setattr(qbo_api, "_api", fake)
    with Session(engine) as s:
        customer = models.Customer(name="First Name")
        s.add(customer)
        s.commit()
        with pytest.raises(qbo_api.QboError):
            qbo_api.push_customer(s, customer)
        customer.name = "Changed Name"
        s.commit()
        assert qbo_api.push_customer(s, customer) == "12"
    assert calls[0] == calls[1]


def test_connection_guard_refreshes_stale_setting_identity(engine):
    import pytest
    _connected(engine)
    with Session(engine) as old:
        retained = old.get(models.Setting, "qbo_realm_id")
        assert retained.value == "9130000"
        old.commit()
        # Materialize after commit so the identity is genuinely stale.
        assert retained.value == "9130000"
        with Session(engine) as changed:
            models.set_setting(changed, "qbo_realm_id", "777")
            changed.commit()
        with pytest.raises(qbo_api.QboError, match="connection changed"):
            with qbo_api.connection_guard(old, "sandbox:9130000"):
                raise AssertionError("must not enter stale workflow")


def test_connection_guard_holds_configuration_lock(engine):
    import threading
    _connected(engine)
    attempted = threading.Event()
    acquired = threading.Event()
    def change_configuration():
        attempted.set()
        with qbo_api._refresh_lock:
            acquired.set()
    with Session(engine) as s:
        with qbo_api.connection_guard(s, "sandbox:9130000"):
            worker = threading.Thread(target=change_configuration)
            worker.start()
            assert attempted.wait(1)
            assert not acquired.is_set()
        worker.join(1)
        assert acquired.is_set()


def test_refund_receipts_paginate(engine, monkeypatch):
    calls = []
    def fake(s, method, path, **kwargs):
        statement = urllib.parse.unquote(path)
        calls.append(statement)
        return {"QueryResponse": {"RefundReceipt": [{"Id": "8"}] if len(calls) == 1 else []}}
    monkeypatch.setattr(qbo_api, "_api", fake)
    with Session(engine) as s:
        assert qbo_api.query_refund_receipts(s, page_size=1) == [{"Id": "8"}]
    assert "from RefundReceipt" in calls[0]
    assert "startposition 2" in calls[1]

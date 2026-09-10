"""QuickBooks OAuth and accounting transport. Financial mutations are explicit;
callers persist approval and request IDs before invoking them. No automatic retries.
"""
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from functools import wraps

from app import models

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"

_API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

# In-process access-token cache: {"token", "exp"}. Single process (instance
# lock), so a module dict is the right scope.
_cache: dict = {}
_refresh_lock = threading.RLock()


def _connection_locked(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _refresh_lock:
            return function(*args, **kwargs)
    return locked


@contextmanager
def connection_guard(session, expected_scope):
    """Keep one approved company's workflow bound across its reads and writes.

    Configuration writers and OAuth changes use this same process lock. Refresh
    clean setting identities before comparing so a long-lived Session cannot
    authorize a stale company after another request changes the connection.
    """
    with _refresh_lock:
        for obj in list(session.identity_map.values()):
            if (isinstance(obj, models.Setting) and obj.key.startswith("qbo_")
                    and obj not in session.dirty):
                session.expire(obj)
        if connection_scope(session) != expected_scope or not connected(session):
            raise QboError("QuickBooks connection changed. Reload before continuing.")
        yield


def _safe_error(message, session):
    message = str(message)
    for value in (_cfg(session)["refresh_token"], _cfg(session)["client_secret"], _cache.get("token")):
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


def _expires_in(resp: dict) -> int:
    """Token lifetime in seconds, tolerating a malformed/missing value rather
    than 500ing on int()."""
    try:
        return max(60, int(resp.get("expires_in", 3600)))
    except (TypeError, ValueError):
        return 3600


def clear_cache() -> None:
    """Drop the cached access token — call when the app credentials or the
    sandbox/production environment change, or a token minted for the old
    config keeps being sent to the new endpoint for up to an hour."""
    with _refresh_lock:
        _cache.clear()


class QboError(Exception):
    """QuickBooks connection/API failure needing the admin (expired grant,
    revoked, misconfigured, or a QBO validation error). Admin-safe message."""

    def __init__(self, message, *, status_code=None, retryable=False, outcome_unknown=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


# ---------- config ----------

def _cfg(session) -> dict:
    g = models.get_setting
    return {
        "client_id": g(session, "qbo_client_id", ""),
        "client_secret": g(session, "qbo_client_secret", ""),
        "environment": g(session, "qbo_environment", "sandbox"),
        "realm_id": g(session, "qbo_realm_id", ""),
        "refresh_token": g(session, "qbo_refresh_token", ""),
    }


def connection_scope(session) -> str:
    cfg = _cfg(session)
    return f"{cfg['environment']}:{cfg['realm_id']}"


def _cache_key(cfg):
    return (cfg["environment"], cfg["realm_id"], cfg["client_id"],
            hashlib.sha256(cfg["client_secret"].encode()).hexdigest())


def api_base(session) -> str:
    env = models.get_setting(session, "qbo_environment", "sandbox")
    return _API_BASE.get(env, _API_BASE["sandbox"])


def connected(session) -> bool:
    return bool(models.get_setting(session, "qbo_refresh_token", "")
                and models.get_setting(session, "qbo_realm_id", ""))


def configured(session) -> bool:
    c = _cfg(session)
    return bool(c["client_id"] and c["client_secret"])


# ---------- OAuth (authorization-code flow) ----------

def new_code_verifier() -> str:
    """PKCE verifier (RFC 7636). Defense in depth: even if an authorization
    code leaks (referer, shoulder-surf, a proxy log), it cannot be redeemed
    without the verifier that never left this server."""
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(session, redirect_uri: str, state: str, code_verifier: str) -> str:
    cfg = _cfg(session)
    if not cfg["client_id"]:
        raise QboError("Enter your QuickBooks app credentials first.")
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": _code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _token_request(session, form: dict) -> dict:
    cfg = _cfg(session)
    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": "Basic " + auth,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {"error": f"http_{e.code}"}
        return payload
    except Exception as exc:
        raise QboError("Could not reach QuickBooks. Check the network and try again.") from exc


@_connection_locked
def complete_connection(session, code: str, realm_id: str, redirect_uri: str,
                        code_verifier: str = "") -> str:
    """Exchange the auth code for tokens and persist the connection. Returns
    the connected company name. Commits (tokens must survive regardless of the
    request outcome)."""
    if not realm_id.isdigit() or len(realm_id) > 30:
        # Intuit realm ids are decimal. Validated before it is stored and
        # interpolated into API URLs, so no odd path segment can ever reach
        # the request builder.
        raise QboError("QuickBooks returned an unexpected company id.")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    resp = _token_request(session, form)
    if "refresh_token" not in resp or "access_token" not in resp:
        # Guard both: a response with one but not the other would otherwise
        # KeyError into a 500 instead of an admin-readable message.
        raise QboError(_safe_error(resp.get("error_description") or resp.get("error")
                       or "QuickBooks did not return a token.", session))
    if _cfg(session)["realm_id"] != realm_id:
        invalidate_mapping_settings(session)
    models.set_setting(session, "qbo_refresh_token", resp["refresh_token"])
    models.set_setting(session, "qbo_realm_id", realm_id)
    _cache.update(token=resp["access_token"], exp=time.time() + _expires_in(resp) - 120,
                  scope=_cache_key(_cfg(session)))
    session.commit()
    try:
        return company_name(session)
    except QboError:
        return ""


def access_token(session) -> str:
    with _refresh_lock:
        return _access_token_locked(session)


def _access_token_locked(session) -> str:
    cfg = _cfg(session)
    if (_cache.get("scope") == _cache_key(cfg) and _cache.get("token")
            and time.time() < _cache.get("exp", 0)):
        return _cache["token"]
    cfg = _cfg(session)
    if not cfg["refresh_token"] or not cfg["client_id"]:
        raise QboError("QuickBooks is not connected — connect it in Settings.")
    resp = _token_request(session, {
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    })
    if "access_token" not in resp:
        raise QboError(_safe_error(resp.get("error_description") or resp.get("error")
                       or "QuickBooks sign-in expired — reconnect in Settings.", session))
    # Intuit rotates refresh tokens; persist immediately (own commit) so a
    # later failure can't strand the new one.
    if resp.get("refresh_token") and resp["refresh_token"] != cfg["refresh_token"]:
        models.set_setting(session, "qbo_refresh_token", resp["refresh_token"])
        session.commit()
    _cache.update(token=resp["access_token"], exp=time.time() + _expires_in(resp) - 120,
                  scope=_cache_key(_cfg(session)))
    return _cache["token"]


@_connection_locked
def disconnect(session) -> None:
    """Best-effort token revoke at Intuit, then forget the connection locally.
    Caller commits."""
    cfg = _cfg(session)
    if cfg["refresh_token"]:
        try:
            auth = base64.b64encode(
                f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
            req = urllib.request.Request(
                REVOKE_URL, method="POST",
                data=json.dumps({"token": cfg["refresh_token"]}).encode(),
                headers={"Authorization": "Basic " + auth,
                         "Content-Type": "application/json", "Accept": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass  # local disconnect proceeds regardless
    for key in ("qbo_refresh_token", "qbo_realm_id"):
        models.set_setting(session, key, "")
    _cache.clear()


# ---------- API ----------

@_connection_locked
def _api(session, method: str, path: str, body: dict | None = None,
         *, request_id=None, raw=False, content_type="application/json") -> dict | bytes:
    token = access_token(session)
    # Quoted even though realm_id is validated numeric on the way in — same
    # belt-and-braces the Office 365 client applies to its tenant id, so no
    # stored value can ever shape the request path.
    cfg = _cfg(session)
    realm = urllib.parse.quote(cfg["realm_id"], safe="")
    url = f"{_API_BASE.get(cfg['environment'], _API_BASE['sandbox'])}/v3/company/{realm}/{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}minorversion={MINOR_VERSION}"
    if request_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", request_id):
            raise QboError("Invalid QuickBooks request id.")
        url += "&requestid=" + request_id
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/pdf" if raw else "application/json",
        "Content-Type": content_type,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            result = r.read(20 * 1024 * 1024 + 1)
            if len(result) > 20 * 1024 * 1024:
                raise QboError("QuickBooks response exceeds the supported size.", outcome_unknown=method != "GET")
            if raw:
                return result
            parsed = json.loads(result.decode())
            if not isinstance(parsed, dict):
                raise ValueError("Invalid response object")
            return parsed
    except urllib.error.HTTPError as e:
        if e.code == 401:
            clear_cache()  # next explicit attempt refreshes; never retry a send blindly
        try:
            payload = json.loads(e.read().decode())
            fault = payload.get("Fault", {}).get("Error", [{}])[0]
            msg = fault.get("Message") or fault.get("Detail") or f"HTTP {e.code}"
        except Exception:
            msg = f"QuickBooks API error (HTTP {e.code})"
        raise QboError(_safe_error(msg, session), status_code=e.code, retryable=e.code == 429 or e.code >= 500,
                       outcome_unknown=method != "GET" and (e.code >= 500 or e.code == 408)) from None
    except QboError:
        raise
    except Exception as exc:
        raise QboError("QuickBooks response could not be confirmed. Reconcile before retrying.",
                       retryable=True, outcome_unknown=method != "GET") from exc


def _query(session, statement: str) -> list:
    # safe="" so nothing in a statement can leak into the URL structure. NOTE
    # for Phase 2: QBO's query language is injectable — never interpolate a
    # customer name or other user data into `statement` without escaping it.
    resp = _api(session, "GET", "query?query=" + urllib.parse.quote(statement, safe=""))
    qr = resp.get("QueryResponse", {})
    # QueryResponse holds the entity under its type name (Customer, Item, ...).
    for val in qr.values():
        if isinstance(val, list):
            return val
    return []


def company_name(session) -> str:
    resp = _api(session, "GET", f"companyinfo/{_cfg(session)['realm_id']}")
    return resp.get("CompanyInfo", {}).get("CompanyName", "")


def list_items(session) -> list[dict]:
    """Active QBO Items (services/products) for the income-account mapping."""
    rows = _query(session, "select Id, Name, Type from Item where Active = true "
                           "order by Name maxresults 200")
    return [{"id": r.get("Id"), "name": r.get("Name"), "type": r.get("Type")} for r in rows]


# ---------- customer push ----------

def _customer_payload(customer) -> dict:
    display = (customer.qb_display_name or customer.name or "").strip()
    body: dict = {"DisplayName": display}
    if customer.email and "@" in customer.email:
        body["PrimaryEmailAddr"] = {"Address": customer.email}
    if customer.phone:
        body["PrimaryPhone"] = {"FreeFormNumber": customer.phone}
    return body


def push_customer(session, customer) -> str:
    """Create or update the customer in QBO and return its QBO Id. Idempotent:
    an existing qbo_customer_id updates in place (sparse) rather than creating a
    duplicate. Caller sets customer.qbo_customer_id and commits."""
    payload = _customer_payload(customer)
    scope = connection_scope(session)
    if customer.qbo_customer_id and customer.qbo_customer_scope != scope:
        raise QboError("Customer mapping belongs to an unverified or different QuickBooks company. "
                       "Relink it before syncing.")
    if customer.qbo_customer_id:
        # Sparse update needs the current SyncToken.
        existing = _api(session, "GET", f"customer/{customer.qbo_customer_id}")
        token = existing.get("Customer", {}).get("SyncToken", "0")
        payload.update({"Id": customer.qbo_customer_id, "SyncToken": token, "sparse": True})
    if customer.qbo_customer_id:
        resp = _api(session, "POST", "customer", payload)
    else:
        if not customer.id:
            raise QboError("Save the customer before syncing to QuickBooks.")
        request_id = hashlib.sha256(f"customer:{scope}:{customer.id}".encode()).hexdigest()[:40]
        # Freeze the create body before a network attempt. A later edit must not
        # reuse the same request ID with different data after a lost response.
        pending_key = "qbo_customer_create_" + request_id
        frozen = models.get_setting(session, pending_key, "")
        if frozen:
            payload = json.loads(frozen)
        else:
            models.set_setting(session, pending_key, json.dumps(payload, sort_keys=True))
            session.commit()
        resp = _api(session, "POST", "customer", payload, request_id=request_id)
    qbo_id = resp.get("Customer", {}).get("Id", "")
    if not qbo_id:
        raise QboError("QuickBooks did not return a customer id.")
    customer.qbo_customer_scope = scope
    return qbo_id


def _entity_id(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value):
        raise QboError("Invalid QuickBooks entity id.")
    return value


def create_invoice(session, payload: dict, request_id: str) -> dict:
    """Caller must persist the approved payload and stable request ID first.

    NotSet requests no email. Company-level automations must also be disabled
    and verified during setup; this flag is not an automation policy override.
    """
    if not request_id:
        raise QboError("A persisted invoice request id is required.")
    body = dict(payload, EmailStatus="NotSet")
    invoice = _api(session, "POST", "invoice", body, request_id=request_id).get("Invoice")
    if not isinstance(invoice, dict) or not invoice.get("Id"):
        raise QboError("QuickBooks invoice creation could not be confirmed.", outcome_unknown=True)
    return invoice


def get_invoice(session, invoice_id) -> dict:
    return _api(session, "GET", f"invoice/{_entity_id(invoice_id)}?include=invoiceLink").get("Invoice", {})


def send_invoice(session, invoice_id, email=None, request_id=None) -> dict:
    """One send attempt only; caller reconciles any ambiguous delivery result."""
    path = f"invoice/{_entity_id(invoice_id)}/send"
    if email:
        path += "?sendTo=" + urllib.parse.quote(email, safe="")
    result = _api(session, "POST", path, request_id=request_id,
                  content_type="application/octet-stream").get("Invoice")
    if not isinstance(result, dict) or not result.get("Id"):
        raise QboError("QuickBooks invoice delivery could not be confirmed.", outcome_unknown=True)
    return result


def invoice_pdf(session, invoice_id) -> bytes:
    result = _api(session, "GET", f"invoice/{_entity_id(invoice_id)}/pdf", raw=True)
    if not result.startswith(b"%PDF-"):
        raise QboError("QuickBooks did not return a valid invoice PDF.")
    return result


def query_entities(session, entity, start_position=1, max_results=1000) -> list:
    """One bounded page; whitelist identifiers instead of interpolating user SQL."""
    if entity not in {"Invoice", "Payment", "CreditMemo", "RefundReceipt", "Customer", "Item"}:
        raise QboError("Unsupported QuickBooks entity query.")
    if (type(start_position) is not int or start_position < 1
            or type(max_results) is not int or not 1 <= max_results <= 1000):
        raise QboError("Invalid QuickBooks query page.")
    statement = f"select * from {entity} order by Id startposition {start_position} maxresults {max_results}"
    path = "query?query=" + urllib.parse.quote(statement, safe="")
    if entity == "Invoice":
        path += "&include=invoiceLink"
    response = _api(session, "GET", path)
    if not isinstance(response.get("QueryResponse"), dict):
        raise QboError("QuickBooks returned an incomplete query response.")
    rows = response["QueryResponse"].get(entity, [])
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("Id") for row in rows)
            or len({str(row["Id"]) for row in rows}) != len(rows)):
        raise QboError("QuickBooks returned an invalid query page.")
    return rows


def _all_entities(session, entity, page_size=1000):
    rows = []
    position = 1
    seen = set()
    while True:
        page = query_entities(session, entity, position, page_size)
        ids = {str(row["Id"]) for row in page}
        if ids & seen or position > 1000000:
            raise QboError("QuickBooks query changed during pagination. Sync again.")
        seen.update(ids)
        rows.extend(page)
        if len(page) < page_size:
            return rows
        position += page_size


def query_invoices(session, page_size=1000):
    return _all_entities(session, "Invoice", page_size)


def query_payments(session, page_size=1000):
    return _all_entities(session, "Payment", page_size)


def query_credit_memos(session, page_size=1000):
    return _all_entities(session, "CreditMemo", page_size)


def invalidate_mapping_settings(session):
    for key in ("qbo_item_service_id", "qbo_item_chem_id", "qbo_item_scope",
                "qbo_billing_enabled", "qbo_billing_scope", "qbo_company_name"):
        models.set_setting(session, key, "")


def company_info(session):
    return _api(session, "GET", f"companyinfo/{_entity_id(_cfg(session)['realm_id'])}").get("CompanyInfo", {})


def preferences(session):
    return _api(session, "GET", "preferences").get("Preferences", {})


def get_customer(session, customer_id):
    return _api(session, "GET", f"customer/{_entity_id(customer_id)}").get("Customer", {})


def query_refund_receipts(session, page_size=1000):
    return _all_entities(session, "RefundReceipt", page_size)

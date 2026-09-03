"""QuickBooks Online integration — Phase 1: OAuth 2.0 connect, company-info
connection test, and idempotent customer push.

Auth mirrors app/msoauth.py's discipline (only a refresh token is stored, never
a password; access tokens cache in-process; rotated refresh tokens persist
immediately; errors are admin-safe and never carry a token) — but Intuit uses
the OAuth 2.0 *authorization-code* grant (a browser redirect), not the device
flow, so the connect step round-trips through Intuit's consent screen back to
/settings/qbo/callback.

The app is the source of truth. Phase 1 only READS company info + items and
CREATES/UPDATES customers (non-money). Invoice/payment posting is Phase 2 and
will carry its own idempotency mapping. Uses stdlib urllib (no new dependency).
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from app import models

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "73"

_API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

# In-process access-token cache: {"token", "exp"}. Single process (instance
# lock), so a module dict is the right scope.
_cache: dict = {}


class QboError(Exception):
    """QuickBooks connection/API failure needing the admin (expired grant,
    revoked, misconfigured, or a QBO validation error). Admin-safe message."""


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

def authorize_url(session, redirect_uri: str, state: str) -> str:
    cfg = _cfg(session)
    if not cfg["client_id"]:
        raise QboError("Enter your QuickBooks app credentials first.")
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
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


def complete_connection(session, code: str, realm_id: str, redirect_uri: str) -> str:
    """Exchange the auth code for tokens and persist the connection. Returns
    the connected company name. Commits (tokens must survive regardless of the
    request outcome)."""
    resp = _token_request(session, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })
    if "refresh_token" not in resp:
        raise QboError(resp.get("error_description") or resp.get("error")
                       or "QuickBooks did not return a token.")
    models.set_setting(session, "qbo_refresh_token", resp["refresh_token"])
    models.set_setting(session, "qbo_realm_id", realm_id)
    _cache.update(token=resp["access_token"],
                  exp=time.time() + int(resp.get("expires_in", 3600)) - 120)
    session.commit()
    try:
        return company_name(session)
    except QboError:
        return ""


def access_token(session) -> str:
    if _cache.get("token") and time.time() < _cache.get("exp", 0):
        return _cache["token"]
    cfg = _cfg(session)
    if not cfg["refresh_token"] or not cfg["client_id"]:
        raise QboError("QuickBooks is not connected — connect it in Settings.")
    resp = _token_request(session, {
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    })
    if "access_token" not in resp:
        raise QboError(resp.get("error_description") or resp.get("error")
                       or "QuickBooks sign-in expired — reconnect in Settings.")
    # Intuit rotates refresh tokens; persist immediately (own commit) so a
    # later failure can't strand the new one.
    if resp.get("refresh_token") and resp["refresh_token"] != cfg["refresh_token"]:
        models.set_setting(session, "qbo_refresh_token", resp["refresh_token"])
        session.commit()
    _cache.update(token=resp["access_token"],
                  exp=time.time() + int(resp.get("expires_in", 3600)) - 120)
    return _cache["token"]


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

def _api(session, method: str, path: str, body: dict | None = None) -> dict:
    token = access_token(session)
    realm = _cfg(session)["realm_id"]
    url = f"{api_base(session)}/v3/company/{realm}/{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}minorversion={MINOR_VERSION}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
            fault = payload.get("Fault", {}).get("Error", [{}])[0]
            msg = fault.get("Message") or fault.get("Detail") or f"HTTP {e.code}"
        except Exception:
            msg = f"QuickBooks API error (HTTP {e.code})"
        raise QboError(msg) from None
    except QboError:
        raise
    except Exception as exc:
        raise QboError("Could not reach QuickBooks. Try again in a moment.") from exc


def _query(session, statement: str) -> list:
    resp = _api(session, "GET", "query?query=" + urllib.parse.quote(statement))
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
    if customer.qbo_customer_id:
        # Sparse update needs the current SyncToken.
        existing = _api(session, "GET", f"customer/{customer.qbo_customer_id}")
        token = existing.get("Customer", {}).get("SyncToken", "0")
        payload.update({"Id": customer.qbo_customer_id, "SyncToken": token, "sparse": True})
    resp = _api(session, "POST", "customer", payload)
    qbo_id = resp.get("Customer", {}).get("Id", "")
    if not qbo_id:
        raise QboError("QuickBooks did not return a customer id.")
    return qbo_id

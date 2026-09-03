"""Microsoft 365 "Modern Auth" for SMTP: OAuth 2.0 device-code flow + XOAUTH2.

Basic-auth SMTP is being retired in Exchange Online; this module lets the
owner connect a mailbox without the app ever seeing a password. Flow:

1. Admin registers a public-client app in Microsoft Entra (see README) and
   saves its Client ID + Tenant ID in Settings -> Email.
2. "Connect" starts a device-code flow: Microsoft hands back a short
   user_code the admin types at https://microsoft.com/devicelogin. The
   settings page polls until Microsoft returns tokens.
3. Only the refresh token + account (UPN) are stored (Settings rows). At
   send time access_token() redeems the refresh token — cached in-process
   until near expiry, and Microsoft's rotated refresh tokens are persisted
   immediately so a later crash can't strand a stale one.

Everything uses stdlib urllib (the Geoapify pattern) — no new dependency.
Errors never include tokens; auth failures surface as MsAuthError whose
message is safe to show an admin.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from app import models

SCOPE = "https://outlook.office365.com/SMTP.Send offline_access openid email"
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587

# In-process access-token cache: {"token": str, "exp": epoch}. Single-process
# app (enforced by the instance lock), so a module dict is the right scope.
_cache: dict = {}


class MsAuthError(Exception):
    """Auth is broken in a way that needs the admin (expired grant, consent
    revoked, bad client id). Message is admin-safe: no tokens."""


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{urllib.parse.quote(tenant.strip() or 'organizations')}"


def _post(url: str, data: dict) -> dict:
    """POST form-encoded, return parsed JSON. Microsoft's token endpoint uses
    HTTP 400 for *expected* states (authorization_pending while the admin is
    still typing the code), so HTTPError bodies are parsed, not raised."""
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"http_{e.code}", "error_description": str(e)}


def start_device_flow(client_id: str, tenant: str) -> dict:
    """Begin the device-code flow. Returns Microsoft's response; on success it
    carries user_code, verification_uri, device_code, interval, expires_in."""
    resp = _post(_authority(tenant) + "/oauth2/v2.0/devicecode",
                 {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in resp:
        raise MsAuthError(resp.get("error_description") or resp.get("error")
                          or "Could not start Microsoft sign-in.")
    resp["_client_id"] = client_id
    resp["_tenant"] = tenant
    resp["_deadline"] = time.time() + int(resp.get("expires_in", 900))
    return resp


def poll_device_flow(session, pending: dict) -> str | None:
    """One poll of the token endpoint for an in-flight device flow.

    Returns None while the admin hasn't finished signing in, or the connected
    account (UPN) on success — with the refresh token, account, and
    smtp_auth_mode=oauth365 persisted (committed here: the tokens must
    survive regardless of what the calling request does afterwards).
    Raises MsAuthError when the flow is dead (expired/denied)."""
    if time.time() > pending["_deadline"]:
        raise MsAuthError("The sign-in window expired — start again.")
    resp = _post(_authority(pending["_tenant"]) + "/oauth2/v2.0/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": pending["_client_id"],
        "device_code": pending["device_code"],
    })
    error = resp.get("error", "")
    if error in ("authorization_pending", "slow_down"):
        return None
    if error:
        raise MsAuthError(resp.get("error_description") or error)

    account = _account_from(resp)
    models.set_setting(session, "smtp_oauth_refresh_token", resp["refresh_token"])
    models.set_setting(session, "smtp_oauth_account", account)
    models.set_setting(session, "smtp_auth_mode", "oauth365")
    session.commit()
    _cache.update(token=resp["access_token"],
                  exp=time.time() + int(resp.get("expires_in", 3600)) - 120)
    return account


def _account_from(resp: dict) -> str:
    """The signed-in mailbox address, from the id_token's claims (we request
    openid+email). Payload only — signature verification is unnecessary here:
    the token came straight from Microsoft over TLS and is used as a display
    label + SMTP username, not to authenticate anyone to us."""
    try:
        payload = resp["id_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email") or claims.get("preferred_username") or ""
    except Exception:
        return ""


def access_token(session) -> str:
    """A live access token for SMTP XOAUTH2, refreshing (and persisting any
    rotated refresh token) as needed."""
    if _cache.get("token") and time.time() < _cache.get("exp", 0):
        return _cache["token"]

    g = models.get_setting
    refresh = g(session, "smtp_oauth_refresh_token", "")
    client_id = g(session, "smtp_oauth_client_id", "")
    tenant = g(session, "smtp_oauth_tenant", "")
    if not refresh or not client_id:
        raise MsAuthError("Office 365 is not connected — connect it in Settings.")

    resp = _post(_authority(tenant) + "/oauth2/v2.0/token", {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh,
        "scope": SCOPE,
    })
    if "access_token" not in resp:
        raise MsAuthError(resp.get("error_description") or resp.get("error")
                          or "Microsoft sign-in expired — reconnect in Settings.")
    # Microsoft rotates refresh tokens; persist immediately (own commit) so a
    # failure later in the request can't roll the new one away.
    if resp.get("refresh_token") and resp["refresh_token"] != refresh:
        models.set_setting(session, "smtp_oauth_refresh_token", resp["refresh_token"])
        session.commit()
    _cache.update(token=resp["access_token"],
                  exp=time.time() + int(resp.get("expires_in", 3600)) - 120)
    return _cache["token"]


def disconnect(session) -> None:
    """Forget the connection and fall back to password auth. Caller commits."""
    models.set_setting(session, "smtp_oauth_refresh_token", "")
    models.set_setting(session, "smtp_oauth_account", "")
    models.set_setting(session, "smtp_auth_mode", "password")
    _cache.clear()


def connected(session) -> bool:
    return bool(models.get_setting(session, "smtp_oauth_refresh_token", ""))

"""Cloud connections for water testers and in-pool monitors.

* LabCOM (backend.labcom.cloud GraphQL): PoolLab 1.0/2.0, PoolWaterLAB and
  PrimeLab photometers sync each test to the owner's LabCOM cloud over
  Bluetooth/Wi-Fi. One API token (LabCOM -> user settings) reads every
  LabCOM "account" (customer) the company keeps there.
* Ondilo ICO (interop.ondilo.com, official Customer API, OAuth2): the company
  connects ONE Ondilo login; each homeowner shares their pool with that login
  in the ICO app, and shared pools appear in /pools. No customer password is
  ever stored here.
* iopool (api.iopool.com/v1, official, x-api-key): each homeowner generates
  an API key in their iopool app (More -> Settings -> API key) and gives it to
  the company; the key is stored encrypted with the pool link it serves.

stdlib urllib only, like app/msoauth.py. Error messages are admin-safe and
never include a token or key.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, water_readings

LABCOM_URL = "https://backend.labcom.cloud/graphql"
ONDILO_AUTHORIZE = "https://interop.ondilo.com/oauth2/authorize"
ONDILO_TOKEN = "https://interop.ondilo.com/oauth2/token"
ONDILO_API = "https://interop.ondilo.com/api/customer/v1"
IOPOOL_POOLS = "https://api.iopool.com/v1/pools"

# LabCOM returns an account's whole history; only recent tests are worth
# storing (older ones can't be used on a visit anyway).
LABCOM_LOOKBACK = timedelta(days=7)
# Parameters measured this close together on one tester, for one LabCOM
# account, are one test.
LABCOM_SESSION_GAP = timedelta(minutes=20)
# Ondilo allows 30 requests per hour per user. One /pools call plus one per
# pool, at most hourly, keeps well inside it; beyond this many linked pools
# they are read round-robin.
ONDILO_POOLS_PER_RUN = 25
ONDILO_INTERVAL = timedelta(minutes=55)

_ondilo_cache: dict = {}


class TesterError(Exception):
    """A connection problem to show an admin. Never contains credentials."""


def _http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
          timeout: int = 20):
    """One HTTP call returning (status, parsed JSON or None). Tests replace this."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TesterError(f"Could not reach {urllib.parse.urlsplit(url).netloc} ({type(e).__name__}).") from None


def _utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: str) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if len(v) > 5 and v[-5] in "+-" and v[-3] != ":":   # +0000 -> +00:00
        v = v[:-2] + ":" + v[-2:]
    try:
        return _utc_naive(datetime.fromisoformat(v))
    except ValueError:
        return None


# --- LabCOM ---------------------------------------------------------------------

LABCOM_QUERY = """query openpoolpro {
  CloudAccount {
    Accounts {
      id forename surname street zipcode city
      Measurements { id parameter scenario unit value timestamp device_serial operator_name }
    }
  }
}"""


def labcom_fetch(token: str) -> list[dict]:
    status, data = _http("POST", LABCOM_URL, {
        "Authorization": token, "Content-Type": "application/json", "Accept": "application/json"},
        json.dumps({"query": LABCOM_QUERY}).encode())
    if status in (401, 403):
        raise TesterError("LabCOM rejected the API token. Copy a fresh one from labcom.cloud → user settings.")
    if status != 200 or not isinstance(data, dict):
        raise TesterError(f"LabCOM returned HTTP {status}.")
    if data.get("errors"):
        raise TesterError("LabCOM rejected the request: "
                          + str((data["errors"][0] or {}).get("message", "unknown error"))[:150])
    cloud = (data.get("data") or {}).get("CloudAccount") or {}
    return cloud.get("Accounts") or []


def labcom_ingest(session: Session, accounts: list[dict], now: datetime | None = None) -> int:
    """Group each account's measurements into tests and store them."""
    now = now or models.utcnow()
    created = 0
    for acct in accounts:
        key = str(acct.get("id") or "")
        if not key:
            continue
        name = " ".join(x for x in (acct.get("forename"), acct.get("surname")) if x).strip()
        place = ", ".join(x for x in (acct.get("street"), acct.get("city")) if x)
        label = " — ".join(x for x in (name, place) if x) or f"LabCOM account {key}"
        by_device: dict[str, list] = {}
        for m in acct.get("Measurements") or []:
            try:
                ts = datetime.fromtimestamp(float(m.get("timestamp")), tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if ts < now - LABCOM_LOOKBACK:
                continue
            param = water_readings.param_key(m.get("parameter") or m.get("scenario") or "")
            val = water_readings.parse_number(m.get("value"))
            if param is None or val is None:
                continue
            by_device.setdefault(str(m.get("device_serial") or ""), []).append((ts, str(m.get("id")), param, val))
        for serial, rows in by_device.items():
            rows.sort()
            group: list = []
            for row in rows + [None]:
                if group and (row is None or row[0] - group[-1][0] > LABCOM_SESSION_GAP):
                    first_ts, first_id = group[0][0], group[0][1]
                    values: dict = {}
                    for _, _, param, val in group:
                        values.setdefault(param, val)   # first measurement of a parameter wins
                    _, made = water_readings.upsert(
                        session, source="labcom", external_id=f"{key}:{serial}:{first_id}",
                        measured_at=first_ts, values=values, device_serial=serial,
                        external_key=key, external_label=label)
                    created += made
                    group = []
                if row is not None:
                    group.append(row)
    return created


def labcom_accounts(session: Session) -> list[tuple[str, str]]:
    """(key, label) of LabCOM accounts seen in stored tests, for linking."""
    rows = session.execute(select(models.DeviceReading.external_key, models.DeviceReading.external_label)
                           .where(models.DeviceReading.source == "labcom")
                           .order_by(models.DeviceReading.measured_at.desc())).all()
    seen: dict[str, str] = {}
    for key, label in rows:
        if key and key not in seen:
            seen[key] = label
    return sorted(seen.items(), key=lambda kv: kv[1].lower())


# --- Ondilo ---------------------------------------------------------------------

def ondilo_authorize_url(redirect_uri: str, state: str) -> str:
    return ONDILO_AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": "customer_api", "response_type": "code", "redirect_uri": redirect_uri,
        "scope": "api", "state": state})


def _ondilo_token(form: dict) -> dict:
    status, data = _http("POST", ONDILO_TOKEN, {
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        urllib.parse.urlencode(form).encode())
    if status != 200 or not isinstance(data, dict) or "access_token" not in data:
        raise TesterError("Ondilo sign-in failed — connect Ondilo again.")
    return data


def ondilo_complete(session: Session, code: str, redirect_uri: str) -> None:
    data = _ondilo_token({"code": code, "grant_type": "authorization_code",
                          "client_id": "customer_api", "redirect_uri": redirect_uri})
    if not data.get("refresh_token"):
        raise TesterError("Ondilo did not return a refresh token.")
    models.set_setting(session, "ondilo_refresh_token", data["refresh_token"])
    _ondilo_cache.clear()
    _ondilo_cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)) - 60)


def _ondilo_access(session: Session) -> str:
    if _ondilo_cache.get("token") and _ondilo_cache.get("exp", 0) > time.time():
        return _ondilo_cache["token"]
    refresh = models.get_setting(session, "ondilo_refresh_token", "")
    if not refresh:
        raise TesterError("Ondilo is not connected.")
    data = _ondilo_token({"refresh_token": refresh, "grant_type": "refresh_token",
                          "client_id": "customer_api"})
    if data.get("refresh_token") and data["refresh_token"] != refresh:
        models.set_setting(session, "ondilo_refresh_token", data["refresh_token"])
    _ondilo_cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)) - 60)
    return _ondilo_cache["token"]


def _ondilo_get(session: Session, path: str):
    headers = {"Authorization": f"Bearer {_ondilo_access(session)}", "Accept": "application/json"}
    status, data = _http("GET", ONDILO_API + path, headers)
    if status == 401:
        _ondilo_cache.clear()
        raise TesterError("Ondilo access expired — connect Ondilo again.")
    if status == 429:
        raise TesterError("Ondilo rate limit reached (30 requests per hour); readings will resume next hour.")
    if status != 200:
        raise TesterError(f"Ondilo returned HTTP {status}.")
    return data


def ondilo_pools(session: Session) -> list[dict]:
    """Pools owned by, or shared with, the connected Ondilo login."""
    data = _ondilo_get(session, "/pools")
    out = []
    for p in data or []:
        addr = p.get("address") or {}
        place = ", ".join(x for x in (addr.get("street"), addr.get("city")) if x)
        out.append({"key": str(p.get("id")), "label": " — ".join(x for x in (p.get("name"), place) if x)})
    return out


def ondilo_ingest(session: Session, link: models.TesterLink, measures: list[dict]) -> int:
    values, stamps = {}, []
    names = {"temperature": "temp_c", "ph": "ph", "orp": "orp", "salt": "salt"}
    for m in measures or []:
        key = names.get(m.get("data_type"))
        val = water_readings.parse_number(m.get("value"))
        ts = _iso(m.get("value_time") or "")
        if key and val is not None and ts and m.get("is_valid", True):
            values[key] = val
            stamps.append(ts)
    if not values:
        return 0
    measured = max(stamps)
    _, made = water_readings.upsert(
        session, source="ondilo", kind="monitor", external_id=f"{link.external_key}:{measured.isoformat()}",
        measured_at=measured, values=values, external_key=link.external_key,
        external_label=link.external_label)
    return int(made)


def ondilo_sync(session: Session, now: datetime | None = None, only: list | None = None) -> int:
    now = now or models.utcnow()
    links = only if only is not None else session.scalars(
        select(models.TesterLink).where(models.TesterLink.source == "ondilo")
        .order_by(models.TesterLink.last_polled_at.is_not(None), models.TesterLink.last_polled_at)
        .limit(ONDILO_POOLS_PER_RUN)).all()
    created = 0
    for link in links:
        measures = _ondilo_get(session, f"/pools/{urllib.parse.quote(link.external_key)}/lastmeasures")
        created += ondilo_ingest(session, link, measures)
        link.last_polled_at = now
    return created


# --- iopool ---------------------------------------------------------------------

def iopool_pools(api_key: str) -> list[dict]:
    status, data = _http("GET", IOPOOL_POOLS, {"x-api-key": api_key, "Accept": "application/json"})
    if status in (401, 403):
        raise TesterError("iopool rejected that API key. The owner can copy it from the iopool app: "
                          "More → Settings → API key.")
    if status != 200 or not isinstance(data, list):
        raise TesterError(f"iopool returned HTTP {status}.")
    return data


def iopool_ingest(session: Session, link: models.TesterLink, pool: dict) -> int:
    m = pool.get("latestMeasure") or {}
    ts = _iso(m.get("measuredAt") or "")
    if not ts or not m.get("isValid", True):
        return 0
    values = {k: water_readings.parse_number(m.get(src))
              for k, src in (("temp_c", "temperature"), ("ph", "ph"), ("orp", "orp"))}
    values = {k: v for k, v in values.items() if v is not None}
    if not values:
        return 0
    _, made = water_readings.upsert(
        session, source="iopool", kind="monitor", external_id=f"{link.external_key}:{ts.isoformat()}",
        measured_at=ts, values=values, device_serial=str(m.get("ecoId") or ""),
        external_key=link.external_key, external_label=link.external_label)
    return int(made)


def iopool_sync(session: Session, now: datetime | None = None, only: list | None = None) -> int:
    """One API call per distinct owner key; each returns all that owner's pools."""
    now = now or models.utcnow()
    links = only if only is not None else session.scalars(
        select(models.TesterLink).where(models.TesterLink.source == "iopool")).all()
    by_key: dict[str, list] = {}
    for link in links:
        by_key.setdefault(water_readings.link_secret(link), []).append(link)
    created = 0
    for key, group in by_key.items():
        if not key:
            continue
        pools = {str(p.get("id")): p for p in iopool_pools(key)}
        for link in group:
            if link.external_key in pools:
                created += iopool_ingest(session, link, pools[link.external_key])
            link.last_polled_at = now
    return created


# --- scheduling -----------------------------------------------------------------

def _record(session: Session, source: str, error: str) -> None:
    models.set_setting(session, f"tester_error_{source}", error[:300])
    if not error:
        models.set_setting(session, f"tester_synced_{source}", models.utcnow().isoformat(timespec="seconds"))


def status(session: Session) -> dict:
    return {src: {"error": models.get_setting(session, f"tester_error_{src}", ""),
                  "synced": models.get_setting(session, f"tester_synced_{src}", "")}
            for src in ("labcom", "ondilo", "iopool")}


def sync_all(session: Session, now: datetime | None = None, force_ondilo: bool = False) -> dict:
    """Pull every connected source; one source failing never stops another.
    Returns {source: new readings or error text}."""
    now = now or models.utcnow()
    out: dict = {}
    token = models.get_setting(session, "labcom_token", "")
    if token:
        try:
            out["labcom"] = labcom_ingest(session, labcom_fetch(token), now)
            _record(session, "labcom", "")
        except TesterError as e:
            out["labcom"] = str(e)
            _record(session, "labcom", str(e))
        session.commit()
    has_iopool = session.scalar(select(models.TesterLink.id).where(models.TesterLink.source == "iopool").limit(1))
    if has_iopool:
        try:
            out["iopool"] = iopool_sync(session, now)
            _record(session, "iopool", "")
        except TesterError as e:
            out["iopool"] = str(e)
            _record(session, "iopool", str(e))
        session.commit()
    if models.get_setting(session, "ondilo_refresh_token", ""):
        last = _iso(models.get_setting(session, "tester_ondilo_run", ""))
        if force_ondilo or last is None or now - last >= ONDILO_INTERVAL:
            models.set_setting(session, "tester_ondilo_run", now.isoformat(timespec="seconds"))
            try:
                out["ondilo"] = ondilo_sync(session, now)
                _record(session, "ondilo", "")
            except TesterError as e:
                out["ondilo"] = str(e)
                _record(session, "ondilo", str(e))
            session.commit()
    return out


def refresh_for_visit(session: Session, visit: models.Visit, now: datetime | None = None) -> None:
    """Freshen what a tech is about to choose from: the LabCOM cloud (their
    handheld test from a minute ago) and this pool's own monitors. Throttled
    so a tech tapping the button repeatedly can't hammer the vendors, and
    silent on failure -- the stored readings are still offered."""
    now = now or models.utcnow()
    last = _iso(models.get_setting(session, "tester_visit_refresh", ""))
    if last is not None and now - last < timedelta(seconds=45):
        return
    models.set_setting(session, "tester_visit_refresh", now.isoformat(timespec="seconds"))
    session.commit()
    token = models.get_setting(session, "labcom_token", "")
    try:
        if token:
            labcom_ingest(session, labcom_fetch(token), now)
        links = session.scalars(select(models.TesterLink).where(
            models.TesterLink.property_id == visit.property_id)).all()
        iop = [lnk for lnk in links if lnk.source == "iopool"]
        if iop:
            iopool_sync(session, now, only=iop)
        ond = [lnk for lnk in links if lnk.source == "ondilo"
               and (lnk.last_polled_at is None or now - lnk.last_polled_at > timedelta(minutes=20))]
        if ond and models.get_setting(session, "ondilo_refresh_token", ""):
            ondilo_sync(session, now, only=ond)
        session.commit()
    except TesterError:
        session.rollback()


def sync_job(engine) -> None:
    with Session(engine) as session:
        sync_all(session)
        water_readings.prune(session)
        session.commit()

"""Optional Geoapify forward geocoding. Tolerant by contract: any failure
(no key, network error, bad payload, no result) returns None and never
raises — callers must treat coordinates as best-effort. `fetch` is injectable
so tests never touch the network."""
import json
import urllib.parse
import urllib.request

_URL = "https://api.geoapify.com/v1/geocode/search"


def _default_fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def geocode(text: str, key: str, *, fetch=_default_fetch) -> tuple[float, float] | None:
    text = (text or "").strip()
    if not text or not (key or "").strip():
        return None
    qs = urllib.parse.urlencode({"text": text, "limit": 1, "apiKey": key})
    try:
        data = fetch(f"{_URL}?{qs}")
        feats = data.get("features") or []
        if not feats:
            return None
        props = feats[0].get("properties") or {}
        lat, lon = props.get("lat"), props.get("lon")
        if lat is None or lon is None:
            return None
        return (float(lat), float(lon))
    except Exception:
        return None

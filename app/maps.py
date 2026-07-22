"""Geoapify static-map thumbnail URL builder. Returns None unless both a key
and coordinates are present, so templates can call it unconditionally."""
import urllib.parse

_BASE = "https://maps.geoapify.com/v1/staticmap"


def static_map_url(lat, lon, key: str, *, width=600, height=300, zoom=16) -> str | None:
    if lat is None or lon is None or not (key or "").strip():
        return None
    params = {
        "style": "osm-bright", "width": width, "height": height, "zoom": zoom,
        "center": f"lonlat:{lon},{lat}",
        "marker": f"lonlat:{lon},{lat};color:%23d33;size:medium",
        "apiKey": key,
    }
    # marker's %23 must survive urlencode; build marker/center raw, encode the rest.
    raw = f"center={params['center']}&marker={params['marker']}"
    rest = urllib.parse.urlencode({k: params[k] for k in ("style", "width", "height", "zoom", "apiKey")})
    return f"{_BASE}?{rest}&{raw}"

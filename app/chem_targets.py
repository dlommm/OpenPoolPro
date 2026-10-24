"""Water targets and the band around each one: the single source of truth.

The target is where a reading should be; the band (a half-width, in the
reading's own units) is how far it may drift before anything happens. The dose
engine suggests nothing inside the band, water alerts flag readings outside
it, and the history chart and visit screen draw it. All of them read these
two settings, so changing either moves every one of them together.

Limits keep a typo from switching dosing off (a band of 50 on pH) or making
it fire on every visit (a band of 0).

Pure apart from the settings read: no FastAPI.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    unit: str
    target: float          # default
    band: float            # default half-width
    decimals: int
    lo: float              # allowed target range; the band may not reach below lo
    hi: float
    band_min: float
    band_max: float


SPECS = (
    Spec("fc", "Free chlorine", "ppm", 4.0, 0.5, 1, 0.0, 10.0, 0.2, 2.0),
    Spec("ph", "pH", "", 7.5, 0.3, 1, 6.8, 8.2, 0.1, 0.6),
    Spec("ta", "Total alkalinity", "ppm", 90.0, 10.0, 0, 40.0, 160.0, 5.0, 40.0),
    Spec("cya", "Cyanuric acid", "ppm", 50.0, 10.0, 0, 0.0, 100.0, 5.0, 30.0),
    Spec("salt", "Salt", "ppm", 3200.0, 200.0, 0, 2000.0, 4500.0, 100.0, 600.0),
)
BY_KEY = {s.key: s for s in SPECS}

DEFAULT_TARGETS = {s.key: s.target for s in SPECS}
DEFAULT_BANDS = {s.key: s.band for s in SPECS}


def problem(key: str, target: float, band: float) -> str:
    """Why this target/band pair can't be saved, or "" when it can."""
    s = BY_KEY[key]
    if not math.isfinite(target):
        return "Enter a number for the target."
    if not s.lo <= target <= s.hi:
        return f"Keep the target between {s.lo:g} and {s.hi:g}."
    if not math.isfinite(band):
        return "Enter a number for the band."
    if not s.band_min <= band <= s.band_max:
        return (f"Keep the band between ±{s.band_min:g} and ±{s.band_max:g}; outside that, "
                "doses would be suggested too often or not at all.")
    if target - band < s.lo:
        return f"The band reaches below {s.lo:g}."
    return ""


def _read(session, prefix: str, defaults: dict) -> dict[str, float]:
    from app import models
    out = {}
    for key, default in defaults.items():
        try:
            v = float(models.get_setting(session, f"{prefix}_{key}", str(default)))
        except (TypeError, ValueError):
            v = default
        out[key] = v if math.isfinite(v) else default
    return out


def current(session) -> tuple[dict[str, float], dict[str, float]]:
    """(targets, bands) as saved, each pair falling back to its defaults when
    the stored values no longer validate -- a stale or hand-edited setting must
    never switch dosing off."""
    targets = _read(session, "target", DEFAULT_TARGETS)
    bands = _read(session, "band", DEFAULT_BANDS)
    for key in DEFAULT_TARGETS:
        if problem(key, targets[key], bands[key]):
            targets[key], bands[key] = DEFAULT_TARGETS[key], DEFAULT_BANDS[key]
    return targets, bands

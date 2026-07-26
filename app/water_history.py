"""Water-chemistry history for a property.

Readings are captured on every visit but were only ever shown for that one
visit, so a pool drifting toward trouble — creeping CYA, chronically low FC, a
salt cell losing output — stayed invisible until it went green. This assembles
the same numbers into a series per metric, with the target band the dosing
engine is already steering toward.

Pure: no FastAPI, so the shaping is testable directly.
"""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

# (attribute, label, unit, sensible axis floor/ceiling). The axis bounds keep a
# pH chart from rendering a flat line: plotted from zero, the whole meaningful
# range of pH is three pixels tall.
METRICS = (
    ("fc", "Free chlorine", "ppm", 0.0, 6.0),
    ("ph", "pH", "", 6.8, 8.2),
    ("ta", "Total alkalinity", "ppm", 0.0, 160.0),
    ("cya", "Stabilizer (CYA)", "ppm", 0.0, 120.0),
    ("salt", "Salt", "ppm", 0.0, 4500.0),
)

# Half-width of the "good" band drawn around each target, in that metric's own
# units. Mirrors the deadbands app/dosing.py already refuses to dose inside, so
# the chart and the dose engine tell the same story.
BAND = {"fc": 0.5, "ph": 0.3, "ta": 10.0, "cya": 10.0, "salt": 200.0}


@dataclass
class Series:
    key: str
    label: str
    unit: str
    points: list[tuple[date, float]] = field(default_factory=list)
    target: float | None = None
    band_lo: float | None = None
    band_hi: float | None = None
    axis_lo: float = 0.0
    axis_hi: float = 1.0

    @property
    def latest(self) -> float | None:
        return self.points[-1][1] if self.points else None

    @property
    def in_band(self) -> bool | None:
        """None when there is no reading or no configured target."""
        if self.latest is None or self.band_lo is None:
            return None
        return self.band_lo <= self.latest <= self.band_hi


def targets(session: Session) -> dict[str, float]:
    """The same target settings the dose engine reads."""
    out = {}
    for key, default in (("fc", "4.0"), ("ph", "7.5"), ("ta", "90"),
                         ("cya", "40"), ("salt", "3200")):
        try:
            out[key] = float(models.get_setting(session, f"target_{key}", default))
        except (TypeError, ValueError):
            out[key] = float(default)
    return out


def for_property(session: Session, property_id: int, limit: int = 26) -> list[Series]:
    """Most recent `limit` visits that carry at least one reading, oldest first.

    Bounded rather than all-time: a trend chart is for spotting drift over a
    season, and an unbounded query on a property with years of history would
    grow without limit for no extra insight.
    """
    visits = session.scalars(
        select(models.Visit)
        .where(models.Visit.property_id == property_id,
               models.Visit.status == "done")
        .order_by(models.Visit.date.desc())
        .limit(limit * 2)).all()

    with_readings = [v for v in visits
                     if any(getattr(v, key) is not None for key, *_ in METRICS)]
    with_readings = list(reversed(with_readings[:limit]))

    tgt = targets(session)
    out: list[Series] = []
    for key, label, unit, floor, ceil in METRICS:
        points = [(v.date, float(getattr(v, key))) for v in with_readings
                  if getattr(v, key) is not None]
        target = tgt.get(key)
        half = BAND.get(key, 0.0)
        values = [val for _, val in points]
        # Widen the axis to fit anything outside the nominal range — an
        # out-of-range reading is exactly the one worth seeing.
        axis_lo = min([floor] + values) if values else floor
        axis_hi = max([ceil] + values) if values else ceil
        if target is not None:
            axis_lo = min(axis_lo, target - half)
            axis_hi = max(axis_hi, target + half)
        out.append(Series(
            key=key, label=label, unit=unit, points=points, target=target,
            band_lo=(target - half) if target is not None else None,
            band_hi=(target + half) if target is not None else None,
            axis_lo=axis_lo, axis_hi=axis_hi if axis_hi > axis_lo else axis_lo + 1))
    return [s for s in out if s.points]

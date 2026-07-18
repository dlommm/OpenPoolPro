from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


@dataclass
class Stop:
    property: models.Property
    visit: models.Visit | None
    status: str
    moved_in: bool = False


@dataclass
class MissedStop:
    property: models.Property
    date: date
    visit: models.Visit | None


def is_due(prop: models.Property, on_date: date) -> bool:
    if prop.frequency == "none" or not prop.active or prop.weekday != on_date.weekday():
        return False
    if prop.frequency == "weekly":
        return True
    if prop.anchor_date is None:
        return False
    weeks = (on_date - prop.anchor_date).days // 7
    mod = 2 if prop.frequency == "biweekly" else 4
    return weeks % mod == 0


def _visits_on(session: Session, d: date) -> list[models.Visit]:
    return session.scalars(select(models.Visit).where(models.Visit.date == d)).all()


def stops_for_date(session: Session, d: date) -> list[Stop]:
    visits = {v.property_id: v for v in _visits_on(session, d)}
    # Suppressed: any property with a visit that has original_date == d (the visit moved away)
    suppressed_visits = session.scalars(select(models.Visit).where(models.Visit.original_date == d)).all()
    suppressed = {v.property_id for v in suppressed_visits}
    stops: list[Stop] = []
    props = session.scalars(select(models.Property).where(models.Property.active)).all()
    for p in props:
        v = visits.get(p.id)
        if v is not None and v.original_date != d:
            stops.append(Stop(property=p, visit=v, status=v.status,
                              moved_in=v.original_date is not None))
        elif is_due(p, d) and p.id not in suppressed:
            stops.append(Stop(property=p, visit=None, status="due"))
    stops.sort(key=lambda s: s.property.route_order)
    return stops


def missed_stops(session: Session, today: date, lookback_days: int = 14) -> list[MissedStop]:
    # Collect missed stops from the most recent due date for each property
    most_recent: dict[int, MissedStop] = {}
    found_recent_due: dict[int, bool] = {}  # Track if we found the most recent due date

    for i in range(1, lookback_days + 1):
        d = today - timedelta(days=i)
        for stop in stops_for_date(session, d):
            prop_id = stop.property.id

            # If we've already processed the most recent due date for this property, skip older ones
            if found_recent_due.get(prop_id, False):
                continue

            # We found a due date for this property
            found_recent_due[prop_id] = True

            # Skip done stops and resolved visits
            if stop.status == "done":
                continue
            if stop.visit is not None and stop.visit.resolved:
                continue

            ms = MissedStop(property=stop.property, date=d, visit=stop.visit)
            most_recent[prop_id] = ms

    out = list(most_recent.values())
    out.sort(key=lambda m: m.date)
    return out

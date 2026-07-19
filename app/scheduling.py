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
    if prop.anchor_date is not None and on_date < prop.anchor_date:
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
    props = [p for p in props if p.customer.active]
    for p in props:
        v = visits.get(p.id)
        if v is not None and v.original_date != d:
            stops.append(Stop(property=p, visit=v, status=v.status,
                              moved_in=v.original_date is not None))
        elif is_due(p, d) and p.id not in suppressed:
            stops.append(Stop(property=p, visit=None, status="due"))
    stops.sort(key=lambda s: s.property.route_order)
    return stops


def suggest_tech(session: Session, weekday: int) -> int | None:
    from collections import Counter
    props = session.scalars(select(models.Property).where(
        models.Property.active, models.Property.weekday == weekday,
        models.Property.assigned_tech_id.is_not(None))).all()
    counts = Counter(p.assigned_tech_id for p in props)
    return counts.most_common(1)[0][0] if counts else None


def visible_to(user, assigned_tech_id, active_ids: set[int]) -> bool:
    if user.can("view_all_routes"):
        return True
    if assigned_tech_id in (user.id, None):
        return True
    return assigned_tech_id not in active_ids  # assignee disabled -> treat as unassigned


def missed_stops(session: Session, today: date, lookback_days: int = 14) -> list[MissedStop]:
    latest_unresolved: dict[int, MissedStop] = {}
    for i in range(1, lookback_days + 1):
        d = today - timedelta(days=i)
        for stop in stops_for_date(session, d):
            pid = stop.property.id
            if pid in latest_unresolved:
                continue
            if stop.status == "done":
                continue
            if stop.visit is not None and stop.visit.resolved:
                continue
            latest_unresolved[pid] = MissedStop(property=stop.property, date=d, visit=stop.visit)
    return sorted(latest_unresolved.values(), key=lambda m: m.date)

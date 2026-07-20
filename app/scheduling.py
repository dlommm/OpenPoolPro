import math
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
    effective_tech_id: int | None = None


@dataclass
class MissedStop:
    property: models.Property
    date: date
    visit: models.Visit | None
    effective_tech_id: int | None = None


def active_hold_map(session: Session, d: date) -> set[int]:
    """Prefetch property_ids with a ServiceHold active on date d.

    A hold is active on d iff start_date <= d and (resume_date is None or
    d < resume_date) — resume_date is exclusive (the property resumes ON
    resume_date, so that day is no longer held) and None means indefinite.
    """
    holds = session.scalars(select(models.ServiceHold).where(
        models.ServiceHold.start_date <= d)).all()
    return {h.property_id for h in holds if h.resume_date is None or d < h.resume_date}


def hold_active(session: Session, property_id: int, d: date) -> bool:
    """Single-property convenience check built on active_hold_map — prefer
    active_hold_map directly when checking many properties for the same date."""
    return property_id in active_hold_map(session, d)


def is_due(prop: models.Property, on_date: date, held: bool = False) -> bool:
    if held:
        return False
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


def absence_map(session: Session, d: date) -> dict[int, int | None]:
    """Prefetch absent user_id -> cover_tech_id (or None) for date d.

    Absence active on d iff start_date <= d <= end_date."""
    absences = session.scalars(select(models.TechAbsence).where(
        models.TechAbsence.start_date <= d, models.TechAbsence.end_date >= d)).all()
    return {a.user_id: a.cover_tech_id for a in absences}


def effective_tech_id(assigned_id: int | None, absences: dict[int, int | None]) -> int | None:
    """Resolve who actually covers a stop assigned to assigned_id, given a
    day's absence_map. Pure: never assigned -> None; not absent -> assigned
    unchanged; absent -> the cover tech, unless that cover is also absent
    on this date, in which case the stop falls back to unassigned (None)."""
    if assigned_id is None or assigned_id not in absences:
        return assigned_id
    cover = absences[assigned_id]
    if cover is None or cover in absences:
        return None
    return cover


def stops_for_date(session: Session, d: date) -> list[Stop]:
    visits = {v.property_id: v for v in _visits_on(session, d)}
    # Suppressed: any property with a visit that has original_date == d (the visit moved away)
    suppressed_visits = session.scalars(select(models.Visit).where(models.Visit.original_date == d)).all()
    suppressed = {v.property_id for v in suppressed_visits}
    held_ids = active_hold_map(session, d)
    absences = absence_map(session, d)
    stops: list[Stop] = []
    props = session.scalars(select(models.Property).where(models.Property.active)).all()
    props = [p for p in props if p.customer.active]
    for p in props:
        eff = effective_tech_id(p.assigned_tech_id, absences)
        v = visits.get(p.id)
        if v is not None and v.original_date != d:
            stops.append(Stop(property=p, visit=v, status=v.status,
                              moved_in=v.original_date is not None, effective_tech_id=eff))
        elif is_due(p, d, held=p.id in held_ids) and p.id not in suppressed:
            stops.append(Stop(property=p, visit=None, status="due", effective_tech_id=eff))
    stops.sort(key=lambda s: s.property.route_order)
    return stops


def suggest_tech(session: Session, weekday: int) -> int | None:
    from collections import Counter
    props = session.scalars(select(models.Property).where(
        models.Property.active, models.Property.weekday == weekday,
        models.Property.assigned_tech_id.is_not(None))).all()
    counts = Counter(p.assigned_tech_id for p in props)
    return counts.most_common(1)[0][0] if counts else None


def weekday_load(session: Session) -> dict[int, dict[int | None, list[models.Property]]]:
    """Every scheduled property (frequency != 'none', active property AND
    active customer -- the same filter stops_for_date applies), grouped by
    weekday and then by assigned_tech_id (None key = unassigned). Day-agnostic:
    a property counts once regardless of is_due's biweekly/every4weeks math,
    since balancing is about route headcount, not a specific date's stops."""
    props = session.scalars(select(models.Property).where(
        models.Property.active, models.Property.frequency != "none")).all()
    props = [p for p in props if p.customer.active]
    load: dict[int, dict[int | None, list[models.Property]]] = {}
    for p in props:
        load.setdefault(p.weekday, {}).setdefault(p.assigned_tech_id, []).append(p)
    return load


def balance_moves(load_for_weekday: dict[int | None, list[models.Property]],
                   tech_ids: list[int]) -> list[tuple[models.Property, int]]:
    """Pure, deterministic greedy rebalance for one weekday's load.

    Only entries whose key is in tech_ids or is None (unassigned) count
    towards the target -- properties assigned to a tech NOT in tech_ids
    (e.g. an admin/owner who was assigned by hand, or a disabled tech) are
    left untouched and out of the headcount, so balancing never disturbs a
    manual override.

    target = ceil(count / len(tech_ids)) where count is that headcount.

    Phase 1 -- unassigned properties (in ascending route_order, then id, for
    determinism) are each handed to whichever tech currently has the lowest
    count, ties broken by ascending tech id.

    Phase 2 -- for every tech left over target (processed highest-count
    first, ties by ascending tech id), its highest-route_order properties
    (ties by descending id) are moved one at a time to the currently
    lowest-count under-target tech (ties by ascending tech id) until it is
    back at target.

    Returns only the moves needed: (property, new_tech_id) pairs. Preview
    and Apply both call this function on a fresh weekday_load(session), so
    two calls against an unchanged database always agree.
    """
    if not tech_ids:
        return []

    counts = {t: len(load_for_weekday.get(t, [])) for t in tech_ids}
    total = sum(counts.values()) + len(load_for_weekday.get(None, []))
    target = math.ceil(total / len(tech_ids))

    moves: list[tuple[models.Property, int]] = []

    unassigned = sorted(load_for_weekday.get(None, []), key=lambda p: (p.route_order, p.id))
    for p in unassigned:
        dest = min(tech_ids, key=lambda t: (counts[t], t))
        moves.append((p, dest))
        counts[dest] += 1

    pools = {t: sorted(load_for_weekday.get(t, []), key=lambda p: (-p.route_order, -p.id))
             for t in tech_ids}
    over_order = sorted((t for t in tech_ids if counts[t] > target), key=lambda t: (-counts[t], t))
    for t in over_order:
        pool = pools[t]
        idx = 0
        while counts[t] > target and idx < len(pool):
            under = [u for u in tech_ids if counts[u] < target]
            if not under:
                break
            dest = min(under, key=lambda u: (counts[u], u))
            moves.append((pool[idx], dest))
            counts[t] -= 1
            counts[dest] += 1
            idx += 1

    return moves


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
        held_ids = active_hold_map(session, d)
        for stop in stops_for_date(session, d):
            pid = stop.property.id
            if pid in latest_unresolved:
                continue
            if pid in held_ids:
                continue  # date is inside a hold window — not a miss, keep looking further back
            if stop.status == "done":
                continue
            if stop.visit is not None and stop.visit.resolved:
                continue
            latest_unresolved[pid] = MissedStop(property=stop.property, date=d, visit=stop.visit,
                                                effective_tech_id=stop.effective_tech_id)
    return sorted(latest_unresolved.values(), key=lambda m: m.date)

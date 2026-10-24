import logging
from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import inventory, models
from app.db import get_session
from app.scheduling import (ScheduleWindow, is_due, missed_stops, progress_segments,
                            stops_for_date, visible_to)
from app.templating import templates
from app.timefmt import business_today

log = logging.getLogger("openpoolpro.app")
router = APIRouter()


def _parse_backup_remaining(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None  # non-int: ignore banner rather than 500 or render garbage


def _truthy_flag(s: str | None) -> bool:
    """Tolerant boolean query flag: any non-empty value other than "0" is
    truthy. Never raises -- garbage in the query string just means the
    banner it gates doesn't show, rather than a 500."""
    return s not in (None, "", "0")


# sw.js's offline visit-complete/skip queue redirects here after a queued
# POST (`/?queued=1`) so the app shell can show a "saved offline" banner on
# next load. This is the Today page's only mount.
@router.get("/")
def today_page(request: Request, tech: int | None = None, crew: str | None = None,
               backup_remaining: str | None = None,
               queued: str | None = None,
               session: Session = Depends(get_session)):
    """Today, in one of two views (design v0.5):

    one route -- a technician's own stops, or, for someone who can see every
    route, the one chosen with ?tech=. People who hold a route themselves
    (owners included) open on their own; a next-stop card leads.
    whole crew -- ?crew=1, or the default for someone who sees every route
    and has no stops today: progress per person and every stop by person.
    """
    user = request.state.user
    d = business_today()
    # One window covers today AND the 14-day missed-stop lookback, so the two
    # calls below share a single set of queries instead of each re-fetching
    # every active property, hold and absence per day.
    window = ScheduleWindow(session, d - timedelta(days=14), d)
    stops = stops_for_date(session, d, window=window)
    missed = missed_stops(session, d, window=window)
    jobs = session.scalars(
        select(models.Job).where(models.Job.status == "scheduled",
                                 models.Job.scheduled_date == d)
        # The card renders j.property.customer.name — eager-load both hops or
        # the hottest page in the app lazy-loads 2 queries per scheduled job.
        .options(selectinload(models.Job.property)
                 .selectinload(models.Property.customer))).all()
    active_ids = {u.id for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    tech_counts = Counter(s.effective_tech_id for s in stops if s.effective_tech_id is not None)
    can_all = user.can("view_all_routes")
    all_stops, all_missed = stops, missed
    if not can_all:
        stops = [s for s in stops if visible_to(user, s.effective_tech_id, active_ids)]
        missed = [m for m in missed if visible_to(user, m.effective_tech_id, active_ids)]
        jobs = [j for j in jobs if visible_to(user, j.assigned_tech_id, active_ids)]
    else:
        if tech is None and not _truthy_flag(crew) and tech_counts.get(user.id):
            tech = user.id  # route-holders open on their own route
        if tech is not None:
            stops = [s for s in stops if s.effective_tech_id == tech]
            missed = [m for m in missed if m.effective_tech_id == tech]
            jobs = [j for j in jobs if j.assigned_tech_id == tech]
    one_route = not can_all or tech is not None
    done = sum(1 for s in stops if s.status == "done")
    techs = session.scalars(select(models.User).where(
        models.User.active).order_by(models.User.name)).all() if can_all else []
    tech_by_id = {t.id: t for t in techs}

    # Segments of the progress track, and the next stop: the first one due.
    next_stop = next((s for s in stops if s.status == "due"), None)
    lanes = []
    if can_all and not one_route:
        order = sorted(tech_counts, key=lambda t: (t != user.id, tech_by_id[t].name
                                                    if t in tech_by_id else ""))
        for tid in order:
            mine = [s for s in all_stops if s.effective_tech_id == tid]
            lanes.append({"tech": tech_by_id.get(tid), "id": tid, "stops": mine,
                          "segs": progress_segments(mine),
                          "done": sum(1 for s in mine if s.status != "due")})
    unassigned = [s for s in all_stops if s.effective_tech_id is None] if can_all else []
    # The rest of the crew, for someone working their own route who also
    # answers for everyone else's.
    rest = None
    if can_all and one_route and tech == user.id:
        others = [s for s in all_stops if s.effective_tech_id != user.id]
        rest = {"done": sum(1 for s in others if s.status != "due"), "total": len(others),
                "unassigned": unassigned,
                "missed": [m for m in all_missed if m.effective_tech_id != user.id]}
    held = window.held_on(d)
    route_of = tech if can_all else user.id
    on_hold = [p for p in window.properties if p.id in held and is_due(p, d)
               and (not one_route or p.assigned_tech_id == route_of)]
    return templates.TemplateResponse(request, "today.html", {
        "d": d, "stops": stops, "missed": missed, "jobs": jobs,
        "done": done, "total": len(stops), "techs": techs, "tech_filter": tech,
        "tech_counts": tech_counts, "tech_by_id": tech_by_id,
        "one_route": one_route, "crew_view": can_all and not one_route,
        "segments": progress_segments(stops), "next_stop": next_stop,
        "upcoming": [s for s in stops if s.status == "due" and s is not next_stop],
        "finished": [s for s in stops if s.status != "due"],
        "lanes": lanes, "unassigned": unassigned, "rest": rest, "on_hold": on_hold,
        "low_stock": _low_stock(session),
        "backup_remaining": _parse_backup_remaining(backup_remaining),
        "queued": _truthy_flag(queued)})


def _low_stock(session) -> list:
    """Chemicals low at the shop, for the "before you go" card. Like the nav
    badge, a failing query degrades to nothing rather than 500ing Today."""
    try:
        return inventory.low_stock(session)
    except Exception:
        log.warning("low-stock query failed; Today renders without it", exc_info=True)
        return []

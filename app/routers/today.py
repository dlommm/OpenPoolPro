from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import models
from app.db import get_session
from app.scheduling import ScheduleWindow, missed_stops, stops_for_date, visible_to
from app.templating import templates
from app.timefmt import business_today

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
def today_page(request: Request, tech: int | None = None,
               backup_remaining: str | None = None,
               queued: str | None = None,
               session: Session = Depends(get_session)):
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
    if not user.can("view_all_routes"):
        stops = [s for s in stops if visible_to(user, s.effective_tech_id, active_ids)]
        missed = [m for m in missed if visible_to(user, m.effective_tech_id, active_ids)]
        jobs = [j for j in jobs if visible_to(user, j.assigned_tech_id, active_ids)]
    elif tech is not None:
        stops = [s for s in stops if s.effective_tech_id == tech]
        missed = [m for m in missed if m.effective_tech_id == tech]
        jobs = [j for j in jobs if j.assigned_tech_id == tech]
    done = sum(1 for s in stops if s.status == "done")
    techs = session.scalars(select(models.User).where(
        models.User.active).order_by(models.User.name)).all() if user.can("view_all_routes") else []
    return templates.TemplateResponse(request, "today.html", {
        "d": d, "stops": stops, "missed": missed, "jobs": jobs,
        "done": done, "total": len(stops), "techs": techs, "tech_filter": tech,
        "tech_counts": tech_counts, "tech_by_id": {t.id: t for t in techs},
        "backup_remaining": _parse_backup_remaining(backup_remaining),
        "queued": _truthy_flag(queued)})

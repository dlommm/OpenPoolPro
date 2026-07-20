from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session
from app.main import templates
from app.scheduling import missed_stops, stops_for_date, visible_to

router = APIRouter()


def business_today() -> date:
    return datetime.now(config.tz()).date()


def _parse_backup_remaining(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None  # non-int: ignore banner rather than 500 or render garbage


@router.get("/")
def today_page(request: Request, tech: int | None = None,
               backup_remaining: str | None = None,
               session: Session = Depends(get_session)):
    user = request.state.user
    d = business_today()
    stops = stops_for_date(session, d)
    missed = missed_stops(session, d)
    jobs = session.scalars(
        select(models.Job).where(models.Job.status == "scheduled",
                                 models.Job.scheduled_date == d)).all()
    active_ids = {u.id for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    if not user.can("view_all_routes"):
        stops = [s for s in stops if visible_to(user, s.property.assigned_tech_id, active_ids)]
        missed = [m for m in missed if visible_to(user, m.property.assigned_tech_id, active_ids)]
        jobs = [j for j in jobs if visible_to(user, j.assigned_tech_id, active_ids)]
    elif tech is not None:
        stops = [s for s in stops if s.property.assigned_tech_id == tech]
        missed = [m for m in missed if m.property.assigned_tech_id == tech]
        jobs = [j for j in jobs if j.assigned_tech_id == tech]
    done = sum(1 for s in stops if s.status == "done")
    techs = session.scalars(select(models.User).where(
        models.User.active).order_by(models.User.name)).all() if user.can("view_all_routes") else []
    return templates.TemplateResponse(request, "today.html", {
        "d": d, "stops": stops, "missed": missed, "jobs": jobs,
        "done": done, "total": len(stops), "techs": techs, "tech_filter": tech,
        "backup_remaining": _parse_backup_remaining(backup_remaining)})

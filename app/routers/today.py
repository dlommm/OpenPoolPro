from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session
from app.main import templates
from app.scheduling import missed_stops, stops_for_date

router = APIRouter()


def business_today() -> date:
    return datetime.now(config.tz()).date()


@router.get("/")
def today_page(request: Request, session: Session = Depends(get_session)):
    d = business_today()
    stops = stops_for_date(session, d)
    missed = missed_stops(session, d)
    jobs = session.scalars(
        select(models.Job).where(models.Job.status == "scheduled",
                                 models.Job.scheduled_date == d)).all()
    done = sum(1 for s in stops if s.status == "done")
    return templates.TemplateResponse(request, "today.html", {
        "d": d, "stops": stops, "missed": missed, "jobs": jobs,
        "done": done, "total": len(stops)})

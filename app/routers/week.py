from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session, get_or_404
from app.main import templates
from app.scheduling import stops_for_date

router = APIRouter()


@router.get("/week")
def week_page(request: Request, start: date | None = None,
              session: Session = Depends(get_session)):
    if start is None:
        today = datetime.now(config.tz()).date()
        start = today - timedelta(days=today.weekday())
    days = [(start + timedelta(days=i)) for i in range(7)]
    day_stops = {d: stops_for_date(session, d) for d in days}
    return templates.TemplateResponse(request, "week.html", {
        "start": start, "days": days, "day_stops": day_stops,
        "prev": start - timedelta(days=7), "next": start + timedelta(days=7)})


@router.post("/week/move")
def move(property_id: int = Form(...), from_date: date = Form(...),
         to_date: date = Form(...), mode: str = Form("once"),
         session: Session = Depends(get_session)):
    p = get_or_404(session, models.Property, property_id)
    if to_date == from_date:
        pass  # no-op: moving a stop to its own day must not mutate anything
    elif mode == "forever":
        p.weekday, p.anchor_date = to_date.weekday(), to_date
    else:
        exists = session.scalars(select(models.Visit).where(
            models.Visit.property_id == property_id,
            models.Visit.date == to_date)).first()
        if exists is None:
            session.add(models.Visit(property_id=property_id, date=to_date,
                                     status="due", original_date=from_date))
    session.commit()
    start = from_date - timedelta(days=from_date.weekday())
    return RedirectResponse(f"/week?start={start}", status_code=303)


@router.post("/week/reorder")
def reorder(property_id: int = Form(...), direction: str = Form("up"),
            session: Session = Depends(get_session)):
    p = get_or_404(session, models.Property, property_id)
    siblings = session.scalars(select(models.Property)
        .where(models.Property.active, models.Property.weekday == p.weekday)
        .order_by(models.Property.route_order)).all()
    idx = siblings.index(p)
    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(siblings):
        siblings[idx].route_order, siblings[swap].route_order = \
            siblings[swap].route_order, siblings[idx].route_order
    session.commit()
    return RedirectResponse("/week", status_code=303)

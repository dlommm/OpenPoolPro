from collections import Counter
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session, get_or_404
from app.main import templates
from app.scheduling import stops_for_date, visible_to

router = APIRouter()


@router.get("/week")
def week_page(request: Request, start: date | None = None, tech: int | None = None,
              session: Session = Depends(get_session)):
    user = request.state.user
    if start is None:
        today = datetime.now(config.tz()).date()
        start = today - timedelta(days=today.weekday())
    days = [(start + timedelta(days=i)) for i in range(7)]
    active_ids = {u.id for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    techs = session.scalars(select(models.User).where(
        models.User.active).order_by(models.User.name)).all() if user.can("view_all_routes") else []
    tech_counts = Counter()
    day_stops = {}
    for d in days:
        stops = stops_for_date(session, d)
        if user.can("view_all_routes"):
            tech_counts.update(s.effective_tech_id for s in stops if s.effective_tech_id is not None)
        if not user.can("view_all_routes"):
            stops = [s for s in stops if visible_to(user, s.effective_tech_id, active_ids)]
        elif tech is not None:
            stops = [s for s in stops if s.effective_tech_id == tech]
        day_stops[d] = stops
    return templates.TemplateResponse(request, "week.html", {
        "start": start, "days": days, "day_stops": day_stops,
        "prev": start - timedelta(days=7), "next": start + timedelta(days=7),
        "techs": techs, "tech_filter": tech, "tech_counts": tech_counts,
        "tech_by_id": {t.id: t for t in techs}})


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
            # Relocate an existing DUE row sitting at from_date (a stop the tech
            # already opened via _get_or_create, or a prior move's landing row)
            # rather than spawning a second row — two rows would render the
            # property as due on both days and later surface as a phantom miss.
            # Preserve an existing original_date (the chain root) so a chained
            # move keeps suppressing the true origin day.
            src = session.scalars(select(models.Visit).where(
                models.Visit.property_id == property_id,
                models.Visit.date == from_date,
                models.Visit.status == "due")).first()
            if src is not None:
                if src.original_date is None:
                    src.original_date = from_date
                src.date = to_date
            else:
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

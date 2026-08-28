from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inventory, models, qbo
from app.db import get_session
from app.deps import require_admin
from app.main import templates
from app.maintenance import warranty_alerts
from app.routers.today import business_today
from app.scheduling import ScheduleWindow, missed_stops, stops_for_date, visible_to

router = APIRouter()


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_session),
              user=Depends(require_admin)):
    d = business_today()
    rows = qbo.month_rollup(session, d.year, d.month)
    # Shared window for today plus the 14-day lookback — the dashboard was the
    # heaviest page in the app, paying the full scheduler pass 15 times over.
    window = ScheduleWindow(session, d - timedelta(days=14), d)
    stops = stops_for_date(session, d, window=window)
    missed = missed_stops(session, d, window=window)
    open_jobs = session.scalars(select(models.Job).where(
        models.Job.status == "open")).all()
    techs = session.scalars(select(models.User).where(
        models.User.active, models.User.role == "tech")).all()
    crew = []
    for t in techs:
        count = sum(1 for s_ in stops if s_.effective_tech_id == t.id)
        if count:
            crew.append((t, count))
    low_chems = inventory.low_stock(session)
    warranty_items = warranty_alerts(session, d)
    return templates.TemplateResponse(request, "dashboard.html", {
        "revenue_mtd_cents": sum(r.total_cents for r in rows),
        "stops_done": sum(1 for s_ in stops if s_.status == "done"),
        "stops_total": len(stops), "missed_count": len(missed),
        "open_jobs": len(open_jobs),
        "high_jobs": sum(1 for j in open_jobs if j.priority == "high"),
        "crew": crew, "warranty_items": warranty_items, "d": d, "low_chems": low_chems})


@router.get("/today/print")
def route_sheet(request: Request, tech: int | None = None,
                session: Session = Depends(get_session)):
    user = request.state.user
    d = business_today()
    stops = stops_for_date(session, d)
    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "scheduled", models.Job.scheduled_date == d)).all()
    active_ids = {u.id for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    if not user.can("view_all_routes"):
        stops = [s_ for s_ in stops if visible_to(user, s_.property.assigned_tech_id, active_ids)]
        jobs = [j for j in jobs if visible_to(user, j.assigned_tech_id, active_ids)]
    elif tech is not None:
        stops = [s_ for s_ in stops if s_.property.assigned_tech_id == tech]
        jobs = [j for j in jobs if j.assigned_tech_id == tech]
    return templates.TemplateResponse(request, "print_route.html",
                                      {"stops": stops, "jobs": jobs, "d": d})


@router.get("/search")
def search(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = (q or "").strip()[:80]
    customers = properties = []
    if q:
        like = f"%{q}%"
        customers = session.scalars(select(models.Customer).where(
            models.Customer.active,
            models.Customer.name.ilike(like)
            | models.Customer.phone.ilike(like)
            | models.Customer.email.ilike(like)).order_by(models.Customer.name)
            .limit(25)).all()
        properties = session.scalars(select(models.Property).join(models.Customer).where(
            models.Property.active, models.Customer.active,
            models.Property.address.ilike(like) | models.Property.label.ilike(like))
            .order_by(models.Customer.name).limit(25)).all()
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "customers": customers, "properties": properties})

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session, get_or_404
from app.main import templates

router = APIRouter()


def _labor_cents(val: str) -> int:
    try:
        return round(float(val or 0) * 100)
    except ValueError:
        return 0


def _parse_assignee(assigned_tech_id: str) -> int | None:
    if not assigned_tech_id.strip():
        return None
    try:
        return int(assigned_tech_id)
    except ValueError:
        return None


def _eligible_assignees(session: Session, job_type: str):
    users = session.scalars(select(models.User).where(models.User.active)
                            .order_by(models.User.name)).all()
    if job_type == "repair":
        return [u for u in users if u.can("can_repairs")]
    return users


@router.get("/jobs")
def list_jobs(request: Request, status: str = "open",
              session: Session = Depends(get_session)):
    jobs = session.scalars(select(models.Job).where(models.Job.status == status)
                           .order_by(models.Job.priority.asc(), models.Job.id.desc())).all()
    return templates.TemplateResponse(request, "jobs/list.html",
        {"jobs": jobs, "status": status, "show_money": request.state.user.can("view_money")})


@router.get("/jobs/new")
def new_job(request: Request, property_id: int | None = None,
            session: Session = Depends(get_session)):
    props = session.scalars(select(models.Property).where(models.Property.active)
                            .join(models.Customer).order_by(models.Customer.name)).all()
    assignees_all = session.scalars(select(models.User).where(models.User.active)
                                    .order_by(models.User.name)).all()
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": None, "props": props, "selected": property_id,
         "assignees_all": assignees_all,
         "show_money": request.state.user.can("view_money")})


@router.post("/jobs/new")
def create_job(request: Request, property_id: int = Form(...), title: str = Form(...),
               description: str = Form(""), priority: str = Form("normal"),
               labor_dollars: str = Form(""), scheduled_date: str = Form(""),
               job_type: str = Form("service"), assigned_tech_id: str = Form(""),
               session: Session = Depends(get_session)):
    # Validate property exists
    get_or_404(session, models.Property, property_id)
    job_type = "repair" if job_type == "repair" else "service"
    assigned_id = _parse_assignee(assigned_tech_id)
    if assigned_id is not None:
        eligible_ids = {u.id for u in _eligible_assignees(session, job_type)}
        if assigned_id not in eligible_ids:
            return HTMLResponse("That tech can't take this job type", status_code=400)
    j = models.Job(property_id=property_id, title=title, description=description,
                   priority=priority,
                   labor_cents=_labor_cents(labor_dollars) if request.state.user.can("view_money") else 0)
    j.job_type, j.assigned_tech_id = job_type, assigned_id
    if scheduled_date:
        j.scheduled_date = date.fromisoformat(scheduled_date)
        j.status = "scheduled"
    session.add(j)
    session.commit()
    return RedirectResponse(f"/jobs/{j.id}", status_code=303)


@router.get("/jobs/{jid}")
def job_detail(request: Request, jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    photos = session.scalars(select(models.Photo).where(
        models.Photo.entity_type == "job", models.Photo.entity_id == jid)).all()
    return templates.TemplateResponse(request, "jobs/detail.html",
        {"j": j, "photos": photos, "show_money": request.state.user.can("view_money")})


@router.get("/jobs/{jid}/edit")
def edit_job_form(request: Request, jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    props = session.scalars(select(models.Property).where(models.Property.active)
                            .join(models.Customer).order_by(models.Customer.name)).all()
    assignees_all = session.scalars(select(models.User).where(models.User.active)
                                    .order_by(models.User.name)).all()
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": j, "props": props, "selected": j.property_id,
         "assignees_all": assignees_all,
         "show_money": request.state.user.can("view_money")})


@router.post("/jobs/{jid}/edit")
def edit_job(request: Request, jid: int, property_id: int = Form(...), title: str = Form(...),
             description: str = Form(""), priority: str = Form("normal"),
             labor_dollars: str = Form(""), scheduled_date: str = Form(""),
             job_type: str = Form("service"), assigned_tech_id: str = Form(""),
             session: Session = Depends(get_session)):
    # Validate property exists
    get_or_404(session, models.Property, property_id)
    j = get_or_404(session, models.Job, jid)
    job_type = "repair" if job_type == "repair" else "service"
    assigned_id = _parse_assignee(assigned_tech_id)
    if assigned_id is not None:
        eligible_ids = {u.id for u in _eligible_assignees(session, job_type)}
        if assigned_id not in eligible_ids:
            return HTMLResponse("That tech can't take this job type", status_code=400)
    j.property_id, j.title, j.description, j.priority = property_id, title, description, priority
    j.job_type, j.assigned_tech_id = job_type, assigned_id
    if request.state.user.can("view_money"):
        j.labor_cents = _labor_cents(labor_dollars)
    j.scheduled_date = date.fromisoformat(scheduled_date) if scheduled_date else None
    if j.status != "done":
        j.status = "scheduled" if j.scheduled_date else "open"
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)


@router.post("/jobs/{jid}/schedule")
def schedule_job(jid: int, scheduled_date: date = Form(...),
                 session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    j.scheduled_date, j.status = scheduled_date, "scheduled"
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)


@router.post("/jobs/{jid}/complete")
def complete_job(jid: int, request: Request, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    j.status, j.completed_at = "done", models.utcnow()
    j.completed_by_id = request.state.user.id
    session.commit()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{jid}/start")
def start_job(jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    if j.started_at is None:
        j.started_at = models.utcnow()
        session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)


@router.post("/jobs/{jid}/times")
async def job_times(jid: int, request: Request, session: Session = Depends(get_session)):
    from app.timefmt import from_local_hhmm, local_date
    from app.routers.today import business_today
    j = get_or_404(session, models.Job, jid)
    form = await request.form()
    start_anchor = (local_date(j.started_at) if j.started_at
                    else (j.scheduled_date or business_today()))
    started = from_local_hhmm(start_anchor, form.get("started_hhmm"))
    if started is not None:
        j.started_at = started
    if j.completed_at is not None:
        ended = from_local_hhmm(local_date(j.completed_at), form.get("ended_hhmm"))
        if ended is not None:
            j.completed_at = ended
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)


@router.post("/jobs/{jid}/reopen")
def reopen_job(jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    j.status = "scheduled" if j.scheduled_date else "open"
    j.completed_at = None
    j.completed_by_id = None
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)

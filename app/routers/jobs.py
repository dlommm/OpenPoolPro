from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
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


@router.get("/jobs")
def list_jobs(request: Request, status: str = "open",
              session: Session = Depends(get_session)):
    jobs = session.scalars(select(models.Job).where(models.Job.status == status)
                           .order_by(models.Job.priority.asc(), models.Job.id.desc())).all()
    return templates.TemplateResponse(request, "jobs/list.html",
                                      {"jobs": jobs, "status": status})


@router.get("/jobs/new")
def new_job(request: Request, property_id: int | None = None,
            session: Session = Depends(get_session)):
    props = session.scalars(select(models.Property).where(models.Property.active)
                            .join(models.Customer).order_by(models.Customer.name)).all()
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": None, "props": props, "selected": property_id})


@router.post("/jobs/new")
def create_job(property_id: int = Form(...), title: str = Form(...),
               description: str = Form(""), priority: str = Form("normal"),
               labor_dollars: str = Form(""), scheduled_date: str = Form(""),
               session: Session = Depends(get_session)):
    # Validate property exists
    get_or_404(session, models.Property, property_id)
    j = models.Job(property_id=property_id, title=title, description=description,
                   priority=priority,
                   labor_cents=_labor_cents(labor_dollars))
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
                                      {"j": j, "photos": photos})


@router.get("/jobs/{jid}/edit")
def edit_job_form(request: Request, jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    props = session.scalars(select(models.Property).where(models.Property.active)
                            .join(models.Customer).order_by(models.Customer.name)).all()
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": j, "props": props, "selected": j.property_id})


@router.post("/jobs/{jid}/edit")
def edit_job(jid: int, property_id: int = Form(...), title: str = Form(...),
             description: str = Form(""), priority: str = Form("normal"),
             labor_dollars: str = Form(""), scheduled_date: str = Form(""),
             session: Session = Depends(get_session)):
    # Validate property exists
    get_or_404(session, models.Property, property_id)
    j = get_or_404(session, models.Job, jid)
    j.property_id, j.title, j.description, j.priority = property_id, title, description, priority
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
def complete_job(jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    j.status, j.completed_at = "done", models.utcnow()
    session.commit()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{jid}/reopen")
def reopen_job(jid: int, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    j.status = "scheduled" if j.scheduled_date else "open"
    j.completed_at = None
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)

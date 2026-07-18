from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session, get_or_404
from app.main import templates
from app.rates import effective_rate_cents

router = APIRouter()


def _get_or_create(session: Session, property_id: int, d: date) -> models.Visit:
    v = session.scalars(select(models.Visit).where(
        models.Visit.property_id == property_id, models.Visit.date == d)).first()
    if v is None:
        v = models.Visit(property_id=property_id, date=d, status="due")
        session.add(v)
        session.commit()
    return v


def _f(val: str) -> float | None:
    val = (val or "").strip()
    return float(val) if val else None


@router.get("/visit/{property_id}/{visit_date}")
def visit_form(request: Request, property_id: int, visit_date: date,
               session: Session = Depends(get_session)):
    v = _get_or_create(session, property_id, visit_date)
    chems = session.scalars(select(models.Chemical).where(models.Chemical.active)
                            .order_by(models.Chemical.name)).all()
    tasks = session.scalars(select(models.TaskTemplate).where(models.TaskTemplate.active)
                            .order_by(models.TaskTemplate.sort_order)).all()
    photos = session.scalars(select(models.Photo).where(
        models.Photo.entity_type == "visit", models.Photo.entity_id == v.id)).all()
    return templates.TemplateResponse(request, "visits/form.html", {
        "v": v, "prop": v.property, "chems": chems, "task_templates": tasks,
        "photos": photos, "rate": effective_rate_cents(session, v.property)})


@router.post("/visit/{visit_id}/complete")
async def complete(visit_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    v.fc, v.ph = _f(form.get("fc")), _f(form.get("ph"))
    v.ta, v.cya, v.salt = _f(form.get("ta")), _f(form.get("cya")), _f(form.get("salt"))
    v.tasks_done = form.getlist("tasks")
    v.notes = form.get("notes", "")
    v.status = "done"
    v.completed_at = models.utcnow()
    v.resolved = True
    v.rate_cents = effective_rate_cents(session, v.property)
    for old in list(v.chemicals):
        session.delete(old)
    for cid, amt in zip(form.getlist("chem_id"), form.getlist("chem_amount")):
        amount = _f(amt)
        if not amount:
            continue
        chem = get_or_404(session, models.Chemical, int(cid))
        session.add(models.VisitChemical(
            visit_id=v.id, chemical_id=chem.id, amount=amount,
            unit_cost_cents_frozen=chem.cost_per_unit_cents,
            total_cost_cents=round(amount * chem.cost_per_unit_cents)))
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/visit/{visit_id}/skip")
async def skip(visit_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    v.status = "skipped"
    reason = form.get("reason", "").strip()
    if reason:
        v.notes = (v.notes + "\n" if v.notes else "") + f"Skipped: {reason}"
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/visit/{property_id}/{visit_date}/dismiss")
def dismiss(property_id: int, visit_date: date, session: Session = Depends(get_session)):
    v = _get_or_create(session, property_id, visit_date)
    if v.status == "due":
        v.status = "skipped"
    v.resolved = True
    session.commit()
    return RedirectResponse("/", status_code=303)

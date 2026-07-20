import math
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import dosing, models
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
    if not val:
        return None
    try:
        f = float(val)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


@router.get("/visit/{visit_id}/doses")
def visit_doses(request: Request, visit_id: int,
                fc: str = "", ph: str = "", ta: str = "", cya: str = "", salt: str = "",
                session: Session = Depends(get_session)):
    v = get_or_404(session, models.Visit, visit_id)
    pools = [p for p in v.property.pools if p.active]
    pool = max(pools, key=lambda p: p.gallons, default=None)
    if pool is None or not pool.gallons:
        return templates.TemplateResponse(request, "visits/_doses.html",
                                          {"doses": None, "hint": "Add a pool with dimensions to get dose suggestions"})
    readings = {"fc": _f(fc), "ph": _f(ph), "ta": _f(ta), "cya": _f(cya), "salt": _f(salt)}
    if all(val is None for val in readings.values()):
        return templates.TemplateResponse(request, "visits/_doses.html",
                                          {"doses": None, "hint": "Enter readings to get dose suggestions"})
    try:
        targets = {
            "fc": float(models.get_setting(session, "target_fc", "4.0")),
            "ph": float(models.get_setting(session, "target_ph", "7.5")),
            "ta": float(models.get_setting(session, "target_ta", "90")),
            "cya": float(models.get_setting(session, "target_cya", "50")),
            "salt": float(models.get_setting(session, "target_salt", "3200")),
        }
        if not all(math.isfinite(v) for v in targets.values()):
            raise ValueError("non-finite target")
    except (ValueError, OverflowError):
        targets = {"fc": 4.0, "ph": 7.5, "ta": 90.0, "cya": 50.0, "salt": 3200.0}
    chems = session.scalars(select(models.Chemical).where(models.Chemical.active)).all()
    doses = dosing.recommend(readings, pool.gallons, pool.water_type, chems, targets)
    return templates.TemplateResponse(request, "visits/_doses.html",
                                      {"doses": doses, "hint": None})


@router.get("/visit/{property_id}/{visit_date}")
def visit_form(request: Request, property_id: int, visit_date: date,
               session: Session = Depends(get_session)):
    v = _get_or_create(session, property_id, visit_date)
    # Local import avoids a circular import with app.routers.today.
    from app.routers.today import business_today
    if v.status == "due" and v.started_at is None and business_today() >= v.date:
        v.started_at = models.utcnow()
        session.commit()
    chems = session.scalars(select(models.Chemical).where(models.Chemical.active)
                            .order_by(models.Chemical.name)).all()
    # A property with its own active checklist overrides replaces the global
    # TaskTemplate list entirely for this visit's form.
    tasks = session.scalars(select(models.PropertyTask).where(
        models.PropertyTask.property_id == property_id, models.PropertyTask.active
    ).order_by(models.PropertyTask.sort_order)).all()
    if not tasks:
        tasks = session.scalars(select(models.TaskTemplate).where(models.TaskTemplate.active)
                                .order_by(models.TaskTemplate.sort_order)).all()
    photos = session.scalars(select(models.Photo).where(
        models.Photo.entity_type == "visit", models.Photo.entity_id == v.id)).all()
    existing = {vc.chemical_id: vc for vc in v.chemicals}
    return templates.TemplateResponse(request, "visits/form.html", {
        "v": v, "prop": v.property, "chems": chems, "task_templates": tasks,
        "photos": photos, "rate": effective_rate_cents(session, v.property),
        "existing": existing, "show_money": request.state.user.can("view_money")})


@router.post("/visit/{visit_id}/complete")
async def complete(visit_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    v.fc, v.ph = _f(form.get("fc")), _f(form.get("ph"))
    v.ta, v.cya, v.salt = _f(form.get("ta")), _f(form.get("cya")), _f(form.get("salt"))
    v.tasks_done = form.getlist("tasks")
    v.notes = form.get("notes", "")
    v.status = "done"
    from app.routers.today import business_today
    from app.timefmt import from_local_hhmm, local_date

    # Product decision: times are minute-grained; an unedited prefilled form re-writing
    # started_at at minute precision is accepted (minutes are the billing/analytics grain).
    # Anchor each edit to the business-local date of the timestamp it edits
    # (NOT v.date): a missed Monday visit completed Wednesday must keep
    # Wednesday timestamps, or durations fabricate multi-day spans.
    start_anchor = local_date(v.started_at) if v.started_at else business_today()
    started = from_local_hhmm(start_anchor, form.get("started_hhmm"))
    if started is not None:
        v.started_at = started
    end_anchor = local_date(v.completed_at) if v.completed_at else business_today()
    ended = from_local_hhmm(end_anchor, form.get("ended_hhmm"))
    if ended is not None:
        v.completed_at = ended
    elif v.completed_at is None:
        v.completed_at = models.utcnow()
    # A visit completed without ever being opened keeps started_at=None (unknown), by design — duration stays None and analytics must tolerate it.
    v.completed_by_id = request.state.user.id
    v.resolved = True
    v.rate_cents = effective_rate_cents(session, v.property)
    # Preserve frozen history: a prior line's frozen unit cost must survive a
    # re-complete (e.g. fixing a pH typo) even if the catalog price moved on.
    # Only genuinely new lines freeze the current catalog cost.
    prior = {vc.chemical_id: vc.unit_cost_cents_frozen for vc in v.chemicals}
    for old in list(v.chemicals):
        session.delete(old)
    for cid, amt in zip(form.getlist("chem_id"), form.getlist("chem_amount")):
        amount = _f(amt)
        if amount is None or amount <= 0:
            continue
        try:
            chem = session.get(models.Chemical, int(cid))
        except (TypeError, ValueError):
            continue
        if chem is None:
            continue
        frozen = prior.get(chem.id, chem.cost_per_unit_cents)
        session.add(models.VisitChemical(
            visit_id=v.id, chemical_id=chem.id, amount=amount,
            unit_cost_cents_frozen=frozen,
            total_cost_cents=round(amount * frozen)))
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/visit/{visit_id}/skip")
async def skip(visit_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    v.status = "skipped"
    v.completed_by_id = request.state.user.id
    reason = form.get("reason", "").strip()
    if reason:
        v.notes = (v.notes + "\n" if v.notes else "") + f"Skipped: {reason}"
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/visit/{property_id}/{visit_date}/dismiss")
def dismiss(property_id: int, visit_date: date, request: Request, session: Session = Depends(get_session)):
    v = _get_or_create(session, property_id, visit_date)
    if v.status == "due":
        v.status = "skipped"
    v.completed_by_id = request.state.user.id
    v.resolved = True
    session.commit()
    return RedirectResponse("/", status_code=303)

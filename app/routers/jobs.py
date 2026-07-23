from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, rates, sizes, tax
from app.db import get_session, get_or_404
from app.main import templates
from app.routers.customers import _apply_property
from app.routers.pools import _apply as _apply_pool

router = APIRouter()


def _template_size_rows(tmpl: "models.JobTemplate") -> list[dict]:
    """A template's size->hours rows as JSON-able dicts, sorted the same way
    `rates.template_hours` resolves them (catch-all/None last) -- lets the
    jobs/form.html template-picker script mirror the server's resolution."""
    rows = sorted(tmpl.prices, key=lambda r: (r.max_gallons is None, r.max_gallons or 0))
    return [{"max_gallons": r.max_gallons, "hours": r.hours} for r in rows]


def _dollars_to_cents(val: str) -> int:
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


def _parse_int_tolerant(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_float_tolerant(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
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
def new_job(request: Request, property_id: int | None = None, template: str = "",
            session: Session = Depends(get_session)):
    props = session.scalars(select(models.Property).where(models.Property.active)
                            .join(models.Customer).order_by(models.Customer.name)).all()
    assignees_all = session.scalars(select(models.User).where(models.User.active)
                                    .order_by(models.User.name)).all()
    template_id = _parse_int_tolerant(template)  # missing/garbage template -> plain form
    tmpl = session.get(models.JobTemplate, template_id) if template_id is not None else None
    default_rate_cents = rates.default_labor_rate_cents(session)
    resolved_hours, hours_status = None, "flat"
    if tmpl is not None and property_id is not None:
        prop = session.get(models.Property, property_id)
        if prop is not None:
            resolved_hours, hours_status = rates.template_hours(
                tmpl, rates.largest_pool_gallons(prop))
    templates_list = session.scalars(
        select(models.JobTemplate).order_by(models.JobTemplate.title)).all()
    template_rows = {t.id: _template_size_rows(t) for t in templates_list}
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": None, "props": props, "selected": property_id,
         "assignees_all": assignees_all,
         "show_money": request.state.user.can("view_money"),
         "template": tmpl, "default_rate_cents": default_rate_cents,
         "resolved_hours": resolved_hours, "hours_status": hours_status,
         "templates_list": templates_list, "template_rows": template_rows,
         "size_presets": sizes.presets_for_form(session),
         "can_manage_customers": request.state.user.can("manage_customers")})


@router.post("/jobs/new")
def create_job(request: Request, mode: str = Form("existing"),
               property_id: str = Form(""), title: str = Form(...),
               description: str = Form(""), priority: str = Form("normal"),
               rate_dollars: str = Form(""), hours: str = Form(""),
               scheduled_date: str = Form(""),
               job_type: str = Form("service"), assigned_tech_id: str = Form(""),
               template_id: str = Form(""),
               customer_name: str = Form(""), customer_phone: str = Form(""),
               customer_email: str = Form(""),
               new_label: str = Form("Home"), new_street: str = Form(""),
               new_city: str = Form(""), new_state: str = Form(""), new_zip: str = Form(""),
               new_gate_code: str = Form(""), new_dog_warning: bool = Form(False),
               new_dog_note: str = Form(""), new_frequency: str = Form("weekly"),
               new_weekday: str = Form("0"),
               label: str = Form("Main pool"), kind: str = Form("pool"),
               shape: str = Form("rectangle"), length_ft: str = Form(""),
               width_ft: str = Form(""), avg_depth_ft: str = Form(""),
               gallons_override: str = Form(""), water_type: str = Form("chlorine"),
               surface: str = Form("plaster"),
               session: Session = Depends(get_session)):
    job_type = "repair" if job_type == "repair" else "service"
    # "new" mode is only honored when the poster can manage customers -- anyone
    # else (e.g. a tech submitting a stale form) silently falls back to the
    # existing-property path, which then 400s on a blank property_id below.
    if mode == "new" and request.state.user.can("manage_customers"):
        customer = models.Customer(name=customer_name.strip(), phone=customer_phone.strip(),
                                   email=customer_email.strip())
        session.add(customer)
        session.flush()  # assign customer.id for the property FK
        new_prop = models.Property(customer_id=customer.id)
        weekday = _parse_int_tolerant(new_weekday) or 0
        _apply_property(new_prop, new_label, new_street, new_city, new_state, new_zip,
                        new_gate_code, new_dog_warning, new_dog_note, "", new_frequency,
                        weekday, "", "", "", allow_rate=False)
        session.add(new_prop)
        session.flush()  # assign new_prop.id for the pool FK
        pool = models.Pool(property_id=new_prop.id)
        pool_form = {"label": label, "kind": kind, "shape": shape,
                     "length_ft": length_ft, "width_ft": width_ft,
                     "avg_depth_ft": avg_depth_ft, "gallons_override": gallons_override,
                     "water_type": water_type, "surface": surface, "notes": ""}
        try:
            _apply_pool(pool, pool_form)
        except (ValueError, TypeError):
            # Nothing has been committed -- the customer/property flushed above
            # roll back with the session on early return (no session.commit()).
            return HTMLResponse("Invalid pool dimensions: must be > 0 and ≤ 100 ft",
                                status_code=400)
        session.add(pool)
        session.flush()
        pid = new_prop.id
    else:
        pid = _parse_int_tolerant(property_id)
        if pid is None:
            return HTMLResponse("Select a customer and property", status_code=400)
    prop = get_or_404(session, models.Property, pid)
    assigned_id = _parse_assignee(assigned_tech_id)
    if assigned_id is not None:
        eligible_ids = {u.id for u in _eligible_assignees(session, job_type)}
        if assigned_id not in eligible_ids:
            return HTMLResponse("That tech can't take this job type", status_code=400)
    can_money = request.state.user.can("view_money")
    tmpl_id = _parse_int_tolerant(template_id)
    tmpl = session.get(models.JobTemplate, tmpl_id) if tmpl_id is not None else None
    rate_cents = (_dollars_to_cents(rate_dollars) if rate_dollars.strip()
                  else rates.default_labor_rate_cents(session))
    # Resolve hours: an explicit hours field wins; otherwise fall back to the
    # template's estimated hours for this pool size. If the template has a size
    # matrix and the customer's pool matches no row, block the job until the
    # owner enters hours. Only money users see the Rate/Hours fields on the
    # form, so only money users get the template-hours resolution and the
    # unmatched-400 — a non-money user (tech) has no field to satisfy it and
    # must not be dead-ended; hours stays 0 for them (labor stays 0).
    hrs = _parse_float_tolerant(hours)
    if hrs is None and tmpl is not None and can_money:
        h, status = rates.template_hours(tmpl, rates.largest_pool_gallons(prop))
        if status in ("matched", "flat"):
            hrs = h
        elif status == "unmatched":
            return HTMLResponse(
                f"This pool size isn't priced in the “{tmpl.title}” template — "
                "enter hours to create the job.", status_code=400)
    if hrs is None:
        hrs = 0
    labor_cents = rates.job_labor_cents(rate_cents, hrs) if can_money else 0
    j = models.Job(property_id=pid, title=title, description=description,
                   priority=priority, labor_cents=labor_cents, hours=hrs)
    j.job_type, j.assigned_tech_id = job_type, assigned_id
    if scheduled_date:
        j.scheduled_date = date.fromisoformat(scheduled_date)
        j.status = "scheduled"
    session.add(j)
    session.flush()  # assign j.id before attaching tasks
    if tmpl is not None:
        j.tasks = [models.JobTask(label=t.label, position=t.position) for t in tmpl.tasks]
    session.commit()
    return RedirectResponse(f"/jobs/{j.id}", status_code=303)


@router.get("/jobs/new/search")
def job_customer_search(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = (q or "").strip()[:80]
    customers = []
    if q:
        like = f"%{q}%"
        customers = session.scalars(select(models.Customer).where(
            models.Customer.active,
            models.Customer.name.ilike(like) | models.Customer.phone.ilike(like)
            | models.Customer.email.ilike(like)).order_by(models.Customer.name).limit(15)).all()
    return templates.TemplateResponse(request, "jobs/_customer_results.html",
                                      {"customers": customers, "q": q})


@router.get("/jobs/new/properties")
def job_customer_properties(request: Request, customer_id: int,
                            session: Session = Depends(get_session)):
    c = get_or_404(session, models.Customer, customer_id)
    props = [p for p in sorted(c.properties, key=lambda p: p.label) if p.active]
    return templates.TemplateResponse(request, "jobs/_property_options.html",
                                      {"c": c, "props": props})


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
    default_rate_cents = rates.default_labor_rate_cents(session)
    return templates.TemplateResponse(request, "jobs/form.html",
        {"j": j, "props": props, "selected": j.property_id,
         "assignees_all": assignees_all,
         "show_money": request.state.user.can("view_money"),
         "template": None, "default_rate_cents": default_rate_cents,
         "resolved_hours": None, "hours_status": "flat"})


@router.post("/jobs/{jid}/edit")
def edit_job(request: Request, jid: int, property_id: int = Form(...), title: str = Form(...),
             description: str = Form(""), priority: str = Form("normal"),
             rate_dollars: str = Form(""), hours: str = Form(""), scheduled_date: str = Form(""),
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
        rate_cents = (_dollars_to_cents(rate_dollars) if rate_dollars.strip()
                      else rates.default_labor_rate_cents(session))
        hrs = _parse_float_tolerant(hours) or 0
        j.hours = hrs
        j.labor_cents = rates.job_labor_cents(rate_cents, hrs)
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
async def complete_job(jid: int, request: Request, session: Session = Depends(get_session)):
    j = get_or_404(session, models.Job, jid)
    form = await request.form()
    j.status, j.completed_at = "done", models.utcnow()
    j.completed_by_id = request.state.user.id
    j.tax_rate_ppm = tax.resolve_rate_ppm(session, j.property.state)
    raw = (form.get("labor_override_dollars") or "").strip()
    if raw:
        try:
            cents = round(float(raw) * 100)
            j.labor_override_cents = cents if cents >= 0 and cents != j.labor_cents else None
        except ValueError:
            j.labor_override_cents = None
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


@router.post("/jobs/{jid}/tasks/{tid}/toggle")
def toggle_task(jid: int, tid: int, session: Session = Depends(get_session)):
    task = get_or_404(session, models.JobTask, tid)
    if task.job_id != jid:
        return HTMLResponse("mismatch", status_code=400)
    task.done = not task.done
    session.commit()
    return RedirectResponse(f"/jobs/{jid}", status_code=303)

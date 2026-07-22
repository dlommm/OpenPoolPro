from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import addresses, geocode, models
from app.db import get_session, get_or_404
from app.deps import require_perm
from app.main import templates
from app.rates import effective_rate_cents
from app.scheduling import suggest_tech
from app.timefmt import from_local_hhmm

router = APIRouter()

COMM_KINDS = ("call", "email", "text", "note")


def _cents(dollars: str) -> int | None:
    dollars = (dollars or "").strip().lstrip("$")
    return None if dollars == "" else round(float(dollars) * 100)


def _parse_date_tolerant(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_int_tolerant(s: str) -> int | None:
    try:
        return int((s or "").strip())
    except ValueError:
        return None


def _customer_photos(session: Session, customer: "models.Customer"
                     ) -> list[tuple["models.Photo", "models.Property | None"]]:
    """All photos reachable from a customer's properties (via visit/pool/job),
    newest first, paired with the property they belong to (None if unresolved).

    Shared by `_timeline` (folds into the merged feed) and the customer-detail
    Photos section (filterable by tag via `?ptag=`).
    """
    props = list(customer.properties)
    prop_by_id = {p.id: p for p in props}
    property_ids = list(prop_by_id.keys())
    if not property_ids:
        return []

    visit_prop_of = dict(session.execute(select(
        models.Visit.id, models.Visit.property_id
    ).where(models.Visit.property_id.in_(property_ids))).all())
    pool_prop_of = dict(session.execute(select(
        models.Pool.id, models.Pool.property_id
    ).where(models.Pool.property_id.in_(property_ids))).all())
    job_prop_of = dict(session.execute(select(
        models.Job.id, models.Job.property_id
    ).where(models.Job.property_id.in_(property_ids))).all())

    photos = session.scalars(select(models.Photo).where(or_(
        and_(models.Photo.entity_type == "visit", models.Photo.entity_id.in_(visit_prop_of.keys())),
        and_(models.Photo.entity_type == "pool", models.Photo.entity_id.in_(pool_prop_of.keys())),
        and_(models.Photo.entity_type == "job", models.Photo.entity_id.in_(job_prop_of.keys())),
    ))).all()

    out = []
    for ph in photos:
        if ph.entity_type == "visit":
            prop_id = visit_prop_of.get(ph.entity_id)
        elif ph.entity_type == "pool":
            prop_id = pool_prop_of.get(ph.entity_id)
        else:
            prop_id = job_prop_of.get(ph.entity_id)
        out.append((ph, prop_by_id.get(prop_id)))
    out.sort(key=lambda pair: pair[0].taken_at, reverse=True)
    return out


def _timeline(session: Session, customer: "models.Customer", user: "models.User",
              photos: list[tuple["models.Photo", "models.Property | None"]] | None = None
              ) -> list[dict]:
    """Merge visits/jobs/photos/comm-log/quotes/invoices for one customer into
    a single newest-first feed capped at 200 entries.

    Each entry: {at: datetime, kind, icon, title, detail, money (cents|None)}.
    Money is computed internally for every kind then zeroed out afterward
    unless the viewing user has view_money -- keeps the per-kind assembly
    below simple and puts the permission check in exactly one place.

    `photos` lets a caller that already gathered `_customer_photos` (e.g. the
    customer-detail route, which also needs them for the Photos section) pass
    the result in so it isn't computed twice per request. Defaults to None,
    in which case this function computes it itself (used directly by tests).
    """
    props = list(customer.properties)
    prop_by_id = {p.id: p for p in props}
    property_ids = list(prop_by_id.keys())
    entries: list[dict] = []

    if property_ids:
        visits = session.scalars(select(models.Visit).where(
            models.Visit.property_id.in_(property_ids),
            models.Visit.status != "due")).all()
        for v in visits:
            if v.completed_at is not None:
                at, date_only = v.completed_at, False
            else:
                # No real instant recorded (e.g. skipped/dismissed without
                # ever being opened) -- fall back to the visit's calendar
                # date, normalized to the UTC instant of its business-local
                # midnight so sorting stays consistent with instant-backed
                # entries. Rendered date-only (no fabricated time-of-day).
                at, date_only = from_local_hhmm(v.date, "00:00"), True
            parts = []
            if v.chemicals:
                n = len(v.chemicals)
                parts.append(f"{n} chemical{'' if n == 1 else 's'}")
            if v.duration_minutes is not None:
                parts.append(f"{v.duration_minutes} min")
            prop = prop_by_id.get(v.property_id)
            label = prop.label if prop else "Visit"
            entries.append({
                "at": at, "date_only": date_only, "kind": "visit", "icon": "check",
                "title": f"{label} — {'Done' if v.status == 'done' else 'Skipped'}",
                "detail": " · ".join(parts), "money": v.rate_cents,
            })

        jobs = session.scalars(select(models.Job).where(
            models.Job.property_id.in_(property_ids))).all()
        for j in jobs:
            if j.completed_at is not None:
                at, date_only = j.completed_at, False
            elif j.started_at is not None:
                at, date_only = j.started_at, False
            elif j.scheduled_date is not None:
                # Same rationale as the visit fallback above: normalize to
                # the UTC instant of business-local midnight, date-only display.
                at, date_only = from_local_hhmm(j.scheduled_date, "00:00"), True
            else:
                continue  # no reliable timestamp yet (bare open job) -- omit from timeline
            prop = prop_by_id.get(j.property_id)
            detail = f"{prop.label} — {j.status}" if prop else j.status
            entries.append({
                "at": at, "date_only": date_only, "kind": "job", "icon": "jobs",
                "title": j.title, "detail": detail, "money": j.labor_cents,
            })

        if photos is None:
            photos = _customer_photos(session, customer)
        for ph, prop in photos:
            title = f"Photo — {ph.tag}" if ph.tag else "Photo"
            detail = ph.caption or (prop.label if prop else "")
            entries.append({
                "at": ph.taken_at, "date_only": False, "kind": "photo", "icon": "camera",
                "title": title, "detail": detail, "money": None,
            })

    comms = session.scalars(select(models.CommLog).where(
        models.CommLog.customer_id == customer.id)).all()
    comm_user_ids = {c.user_id for c in comms if c.user_id is not None}
    comm_users = {}
    if comm_user_ids:
        comm_users = {u.id: u for u in session.scalars(
            select(models.User).where(models.User.id.in_(comm_user_ids))).all()}
    for c in comms:
        who = comm_users.get(c.user_id)
        detail = f"{c.note} — {who.name}" if who else c.note
        entries.append({
            "at": c.at, "date_only": False, "kind": "comm", "icon": "phone",
            "title": c.kind.title(), "detail": detail, "money": None,
        })

    quotes = session.scalars(select(models.Quote).where(
        models.Quote.customer_id == customer.id)).all()
    for q in quotes:
        total = sum(line.amount_cents for line in q.lines)
        entries.append({
            "at": q.created_at, "date_only": False, "kind": "quote", "icon": "money",
            "title": f"Quote: {q.title}", "detail": q.status, "money": total,
        })

    invoices = session.scalars(select(models.Invoice).where(
        models.Invoice.customer_id == customer.id)).all()
    for inv in invoices:
        entries.append({
            "at": inv.generated_at, "date_only": False, "kind": "invoice", "icon": "money",
            "title": f"Invoice {inv.number}",
            "detail": f"{inv.period_year}-{inv.period_month:02d} · {inv.status}",
            "money": inv.total_cents,
        })

    entries.sort(key=lambda e: e["at"], reverse=True)
    entries = entries[:200]
    if not user.can("view_money"):
        for e in entries:
            e["money"] = None
    return entries


@router.get("/customers")
def list_customers(request: Request, q: str = "", tag: str = "", show: str = "",
                   session: Session = Depends(get_session)):
    show_inactive = show == "inactive"
    stmt = select(models.Customer).where(
        models.Customer.active == (not show_inactive)).order_by(models.Customer.name)
    if q:
        stmt = stmt.where(models.Customer.name.ilike(f"%{q}%"))
    customers = session.scalars(stmt).all()
    tag_needle = tag.strip().lower()
    if tag_needle:
        customers = [c for c in customers if tag_needle in (c.tags or [])]
    return templates.TemplateResponse(request, "customers/list.html",
        {"customers": customers, "q": q, "tag": tag, "show_inactive": show_inactive,
         "can_manage": request.state.user.can("manage_customers")})


@router.get("/customers/new")
def new_customer_form(request: Request, user=Depends(require_perm("manage_customers"))):
    return templates.TemplateResponse(request, "customers/form.html", {"c": None})


@router.post("/customers/new")
def create_customer(name: str = Form(...), qb_display_name: str = Form(""),
                    phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                    notify_email: bool = Form(False),
                    session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    c = models.Customer(name=name, qb_display_name=qb_display_name.strip() or name,
                        phone=phone, email=email, notes=notes, notify_email=notify_email)
    session.add(c)
    session.commit()
    return RedirectResponse(f"/customers/{c.id}", status_code=303)


@router.get("/customers/{cid}")
def customer_detail(request: Request, cid: int, ptag: str = "",
                    session: Session = Depends(get_session)):
    from app.routers.today import business_today
    c = get_or_404(session, models.Customer, cid)
    user = request.state.user
    props = sorted(c.properties, key=lambda p: (not p.active, p.label))
    rates = {p.id: effective_rate_cents(session, p) for p in props}
    jobs = session.scalars(
        select(models.Job).join(models.Property)
        .where(models.Property.customer_id == cid, models.Job.status != "done")).all()
    all_photos = _customer_photos(session, c)
    timeline = _timeline(session, c, user, photos=all_photos)

    ptag = ptag if ptag in models.PHOTO_TAGS else ""  # tolerant: unknown -> all
    photos = [(ph, p) for ph, p in all_photos if not ptag or ph.tag == ptag]

    today = business_today()
    active_holds: dict[int, models.ServiceHold] = {}
    if props:
        holds = session.scalars(select(models.ServiceHold).where(
            models.ServiceHold.property_id.in_([p.id for p in props]))).all()
        for h in holds:
            if h.start_date <= today and (h.resume_date is None or today < h.resume_date):
                active_holds[h.property_id] = h

    lifetime_cents = None
    if user.can("view_money"):
        from app.analytics import revenue_by_customer
        rows = revenue_by_customer(session, date(2000, 1, 1), today)
        row = next((r for r in rows if r.customer.id == cid), None)
        lifetime_cents = row.total_cents if row else 0

    return templates.TemplateResponse(request, "customers/detail.html",
        {"c": c, "props": props, "rates": rates, "jobs": jobs, "timeline": timeline,
         "active_holds": active_holds, "lifetime_cents": lifetime_cents,
         "photos": photos, "ptag": ptag,
         "show_money": user.can("view_money"),
         "can_manage": user.can("manage_customers")})


@router.get("/customers/{cid}/edit")
def edit_customer_form(request: Request, cid: int, session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    return templates.TemplateResponse(request, "customers/form.html",
                                      {"c": get_or_404(session, models.Customer, cid)})


@router.post("/customers/{cid}/edit")
def edit_customer(cid: int, name: str = Form(...), qb_display_name: str = Form(""),
                  phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                  notify_email: bool = Form(False),
                  session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    c = get_or_404(session, models.Customer, cid)
    c.name, c.phone, c.email, c.notes = name, phone, email, notes
    c.qb_display_name = qb_display_name.strip() or name
    c.notify_email = notify_email
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.post("/customers/{cid}/deactivate")
def deactivate_customer(cid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Customer, cid).active = False
    session.commit()
    return RedirectResponse("/customers", status_code=303)


@router.post("/customers/{cid}/reactivate")
def reactivate_customer(cid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Customer, cid).active = True
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.post("/customers/{cid}/tags/add")
def add_customer_tags(cid: int, tags: str = Form(""), session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    c = get_or_404(session, models.Customer, cid)
    merged = list(c.tags or [])
    for t in models.normalize_tags(tags):
        if t not in merged and len(merged) < 10:
            merged.append(t)
    c.tags = merged
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.post("/customers/{cid}/tags/remove")
def remove_customer_tag(cid: int, tag: str = Form(...), session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    c = get_or_404(session, models.Customer, cid)
    c.tags = [t for t in (c.tags or []) if t != tag]
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.post("/customers/{cid}/comm/add")
def add_comm_log(cid: int, request: Request, kind: str = Form("note"), note: str = Form(""),
                 session: Session = Depends(get_session),
                 user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Customer, cid)
    if kind not in COMM_KINDS:
        kind = "note"
    session.add(models.CommLog(customer_id=cid, kind=kind, note=note.strip(),
                               user_id=request.state.user.id))
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


def _parse_tech_id(assigned_tech_id: str) -> int | None:
    if not assigned_tech_id.strip():
        return None
    try:
        return int(assigned_tech_id)
    except ValueError:
        return None


def _apply_property(p: models.Property, label, street, city, state, zip, gate_code, dog_warning,
                    dog_note, notes, frequency, weekday, anchor_date, rate_override_dollars,
                    assigned_tech_id, spa_rate_override_dollars="", allow_rate: bool = True):
    p.label, p.gate_code, p.notes = label, gate_code, notes
    p.street, p.city, p.state, p.zip = street.strip(), city.strip(), state.strip(), zip.strip()
    p.address = addresses.compose_address(p.street, p.city, p.state, p.zip)
    p.dog_warning, p.dog_note = dog_warning, dog_note
    p.frequency, p.weekday = frequency, int(weekday)
    p.anchor_date = date.fromisoformat(anchor_date) if anchor_date else None
    if allow_rate:
        p.rate_override_cents = _cents(rate_override_dollars)
        p.spa_rate_override_cents = _cents(spa_rate_override_dollars)
    p.assigned_tech_id = _parse_tech_id(assigned_tech_id)


def _active_techs(session: Session):
    return session.scalars(select(models.User).where(models.User.active)
                           .order_by(models.User.name)).all()


def _property_form_context(session: Session, p: models.Property | None, customer: models.Customer,
                           user: models.User, suggested_tech: int | None = None, **extra) -> dict:
    from app.routers.today import business_today
    ctx = {"p": p, "customer": customer, "techs": _active_techs(session),
           "suggested_tech": suggested_tech, "show_money": user.can("view_money"),
           "today": business_today(), "holds": [], "tasks": [], "rules": []}
    if p is not None:
        ctx["holds"] = session.scalars(select(models.ServiceHold).where(
            models.ServiceHold.property_id == p.id
        ).order_by(models.ServiceHold.start_date.desc())).all()
        ctx["tasks"] = session.scalars(select(models.PropertyTask).where(
            models.PropertyTask.property_id == p.id, models.PropertyTask.active
        ).order_by(models.PropertyTask.sort_order)).all()
        ctx["rules"] = session.scalars(select(models.MaintenanceRule).where(
            models.MaintenanceRule.property_id == p.id
        ).order_by(models.MaintenanceRule.active.desc(), models.MaintenanceRule.next_due)).all()
    ctx.update(extra)
    return ctx


@router.get("/customers/{cid}/properties/new")
def new_property_form(request: Request, cid: int, session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    customer = get_or_404(session, models.Customer, cid)
    ctx = _property_form_context(session, None, customer, request.state.user,
                                 suggested_tech=suggest_tech(session, 0))
    return templates.TemplateResponse(request, "customers/property_form.html", ctx)


@router.post("/customers/{cid}/properties/new")
def create_property(cid: int, request: Request, label: str = Form("Home"),
                    street: str = Form(""), city: str = Form(""), state: str = Form(""),
                    zip: str = Form(""), gate_code: str = Form(""),
                    dog_warning: bool = Form(False), dog_note: str = Form(""),
                    notes: str = Form(""), frequency: str = Form("weekly"),
                    weekday: int = Form(0), anchor_date: str = Form(""),
                    rate_override_dollars: str = Form(""),
                    assigned_tech_id: str = Form(""),
                    spa_rate_override_dollars: str = Form(""),
                    session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    p = models.Property(customer_id=cid)
    _apply_property(p, label, street, city, state, zip, gate_code, dog_warning, dog_note, notes,
                    frequency, weekday, anchor_date, rate_override_dollars, assigned_tech_id,
                    spa_rate_override_dollars, allow_rate=request.state.user.can("view_money"))
    key = models.get_setting(session, "geoapify_key", "")
    if key and p.address:
        try:
            coords = geocode.geocode(p.address, key)
            if coords:
                p.latitude, p.longitude = coords
        except Exception:
            pass
    session.add(p)
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.get("/properties/{pid}/edit")
def edit_property_form(request: Request, pid: int, session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    ctx = _property_form_context(session, p, p.customer, request.state.user)
    return templates.TemplateResponse(request, "customers/property_form.html", ctx)


@router.post("/properties/{pid}/edit")
def edit_property(pid: int, request: Request, label: str = Form("Home"),
                  street: str = Form(""), city: str = Form(""), state: str = Form(""),
                  zip: str = Form(""), gate_code: str = Form(""), dog_warning: bool = Form(False),
                  dog_note: str = Form(""), notes: str = Form(""),
                  frequency: str = Form("weekly"), weekday: int = Form(0),
                  anchor_date: str = Form(""), rate_override_dollars: str = Form(""),
                  assigned_tech_id: str = Form(""),
                  spa_rate_override_dollars: str = Form(""),
                  session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    _apply_property(p, label, street, city, state, zip, gate_code, dog_warning, dog_note, notes,
                    frequency, weekday, anchor_date, rate_override_dollars, assigned_tech_id,
                    spa_rate_override_dollars, allow_rate=request.state.user.can("view_money"))
    key = models.get_setting(session, "geoapify_key", "")
    if key and p.address:
        try:
            coords = geocode.geocode(p.address, key)
            if coords:
                p.latitude, p.longitude = coords
        except Exception:
            pass
    session.commit()
    return RedirectResponse(f"/customers/{p.customer_id}", status_code=303)


@router.post("/properties/{pid}/deactivate")
def deactivate_property(pid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    p.active = False
    session.commit()
    return RedirectResponse(f"/customers/{p.customer_id}", status_code=303)


@router.post("/properties/{pid}/reactivate")
def reactivate_property(pid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    p.active = True
    session.commit()
    return RedirectResponse(f"/customers/{p.customer_id}", status_code=303)


# --- Seasonal holds ----------------------------------------------------------

@router.post("/properties/{pid}/holds/new")
def create_hold(pid: int, request: Request, start_date: str = Form(""),
                resume_date: str = Form(""), reason: str = Form(""),
                session: Session = Depends(get_session),
                user=Depends(require_perm("manage_customers"))):
    from app.routers.today import business_today
    p = get_or_404(session, models.Property, pid)
    start = _parse_date_tolerant(start_date) or business_today()
    resume = _parse_date_tolerant(resume_date)
    if resume is not None and resume <= start:
        ctx = _property_form_context(session, p, p.customer, request.state.user,
            hold_error="Resume date must be after the start date.",
            hold_start=start_date, hold_resume=resume_date, hold_reason=reason)
        return templates.TemplateResponse(request, "customers/property_form.html", ctx,
                                          status_code=400)
    session.add(models.ServiceHold(property_id=pid, start_date=start, resume_date=resume,
                                   reason=reason.strip()))
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


@router.post("/properties/{pid}/holds/{hold_id}/end")
def end_hold(pid: int, hold_id: int, session: Session = Depends(get_session),
            user=Depends(require_perm("manage_customers"))):
    from app.routers.today import business_today
    h = get_or_404(session, models.ServiceHold, hold_id)
    if h.property_id != pid:
        raise HTTPException(status_code=404)
    h.resume_date = business_today()
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


# --- Per-property checklist overrides ----------------------------------------

@router.post("/properties/{pid}/tasks/new")
def add_property_task(pid: int, label: str = Form(""), session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Property, pid)
    label = label.strip()
    if label:
        existing_orders = session.scalars(select(models.PropertyTask.sort_order).where(
            models.PropertyTask.property_id == pid)).all()
        next_order = (max(existing_orders) + 1) if existing_orders else 0
        session.add(models.PropertyTask(property_id=pid, label=label, sort_order=next_order))
        session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


@router.post("/properties/{pid}/tasks/{task_id}/deactivate")
def deactivate_property_task(pid: int, task_id: int, session: Session = Depends(get_session),
                             user=Depends(require_perm("manage_customers"))):
    t = get_or_404(session, models.PropertyTask, task_id)
    if t.property_id != pid:
        raise HTTPException(status_code=404)
    t.active = False
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


@router.post("/properties/{pid}/tasks/{task_id}/move")
def move_property_task(pid: int, task_id: int, direction: str = Form(...),
                       session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    t = get_or_404(session, models.PropertyTask, task_id)
    if t.property_id != pid:
        raise HTTPException(status_code=404)
    siblings = session.scalars(select(models.PropertyTask).where(
        models.PropertyTask.property_id == pid, models.PropertyTask.active
    ).order_by(models.PropertyTask.sort_order)).all()
    idx = next((i for i, sib in enumerate(siblings) if sib.id == t.id), None)
    if idx is not None:
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(siblings):
            other = siblings[swap_idx]
            t.sort_order, other.sort_order = other.sort_order, t.sort_order
            session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


# --- Maintenance rules --------------------------------------------------------

@router.post("/properties/{pid}/rules/new")
def create_rule(pid: int, title: str = Form(...), description: str = Form(""),
               job_type: str = Form("service"), interval_weeks: str = Form(""),
               lead_days: str = Form(""), next_due: str = Form(""),
               session: Session = Depends(get_session),
               user=Depends(require_perm("manage_customers"))):
    from app.routers.today import business_today
    get_or_404(session, models.Property, pid)
    jt = job_type if job_type in ("service", "repair") else "service"
    interval = _parse_int_tolerant(interval_weeks)
    interval = interval if interval is not None and interval >= 1 else 4
    lead = _parse_int_tolerant(lead_days)
    lead = lead if lead is not None and lead >= 0 else 7
    due = _parse_date_tolerant(next_due) or business_today()
    session.add(models.MaintenanceRule(
        property_id=pid, title=title, description=description, job_type=jt,
        interval_weeks=interval, lead_days=lead, next_due=due))
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


@router.post("/properties/{pid}/rules/{rule_id}/edit")
def edit_rule(pid: int, rule_id: int, title: str = Form(...), description: str = Form(""),
             job_type: str = Form("service"), interval_weeks: str = Form(""),
             lead_days: str = Form(""), next_due: str = Form(""),
             session: Session = Depends(get_session),
             user=Depends(require_perm("manage_customers"))):
    r = get_or_404(session, models.MaintenanceRule, rule_id)
    if r.property_id != pid:
        raise HTTPException(status_code=404)
    r.title, r.description = title, description
    if job_type in ("service", "repair"):
        r.job_type = job_type
    interval = _parse_int_tolerant(interval_weeks)
    if interval is not None and interval >= 1:
        r.interval_weeks = interval  # else: tolerant parse -- keep previous value
    lead = _parse_int_tolerant(lead_days)
    if lead is not None and lead >= 0:
        r.lead_days = lead  # else: tolerant parse -- keep previous value
    due = _parse_date_tolerant(next_due)
    if due is not None:
        r.next_due = due  # else: tolerant parse -- keep previous value
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)


@router.post("/properties/{pid}/rules/{rule_id}/deactivate")
def deactivate_rule(pid: int, rule_id: int, session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    r = get_or_404(session, models.MaintenanceRule, rule_id)
    if r.property_id != pid:
        raise HTTPException(status_code=404)
    r.active = False
    session.commit()
    return RedirectResponse(f"/properties/{pid}/edit", status_code=303)

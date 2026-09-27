import math
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import dosing, inventory, mailer, models, qbo, tax, water_readings
from app.db import get_or_404, get_session
from app.rates import effective_rate_cents
from app.templating import templates
from app.timefmt import business_today, from_local_hhmm, local_date

router = APIRouter()


def service_report(session, v, business: str, tech_name: str) -> str:
    """The plain-text visit summary the homeowner receives.

    Proof of work is what answers "did anyone actually come?", justifies a rate
    increase, and settles a dispute — so it includes what was measured and what
    was added, not just that somebody showed up. Everything here is already
    captured on the visit; none of it was ever shown to the customer.

    Deliberately excludes money: this goes out automatically on every visit,
    and pricing conversations belong on the invoice, not in a service note.
    """
    from app import water_history

    tgt = water_history.targets(session)
    lines = [f"{business}", f"{v.property.label} — {v.date.strftime('%B %-d, %Y')}", ""]

    readings = []
    for key, label, unit, *_ in water_history.METRICS:
        value = getattr(v, key, None)
        if value is None:
            continue
        target = tgt.get(key)
        suffix = f" (target {target:g})" if target is not None else ""
        readings.append(f"  {label}: {value:g}{' ' + unit if unit else ''}{suffix}")
    if readings:
        lines += ["Water test:"] + readings + [""]

    # Queried rather than read off v.chemicals: complete() inserts the new lines
    # by visit_id, so the already-loaded relationship collection is stale at
    # this point and would report the pre-save chemicals (or none at all).
    session.flush()
    added = session.scalars(select(models.VisitChemical).where(
        models.VisitChemical.visit_id == v.id)).all()
    if added:
        lines.append("Chemicals added:")
        for vc in added:
            lines.append(f"  {vc.chemical.name}: {vc.amount:g} {vc.chemical.unit}")
        lines.append("")

    if v.tasks_done:
        lines += ["Completed:"] + [f"  {t}" for t in v.tasks_done] + [""]

    if v.notes:
        lines += ["Notes:", f"  {v.notes}", ""]

    lines.append(f"Technician: {tech_name}")
    return "\n".join(lines)


def _get_or_create(session: Session, property_id: int, d: date) -> models.Visit:
    get_or_404(session, models.Property, property_id)
    v = session.scalars(select(models.Visit).where(
        models.Visit.property_id == property_id, models.Visit.date == d)).first()
    if v is None:
        v = models.Visit(property_id=property_id, date=d, status="due")
        session.add(v)
        try:
            session.commit()
        except IntegrityError:
            # Another request created this stop's row between our SELECT and
            # INSERT (unique on property_id+date). Take theirs.
            session.rollback()
            v = session.scalars(select(models.Visit).where(
                models.Visit.property_id == property_id, models.Visit.date == d)).first()
    return v


def queued_owner_conflict(request: Request):
    """Reject an offline replay that belongs to a different staff member.

    Offline queue entries live in IndexedDB, which is per-device, not
    per-login. On a shared phone a tech could go offline, queue a completion,
    hand the phone over, and the next person's session would replay that work
    under THEIR name -- silently wrong attribution in completed_by, the audit
    log and payroll-facing reports. sw.js stamps the queuing identity and
    replays it here; a mismatch is refused before anything is written.

    Returns a 409 response to return, or None to proceed. 409 is deliberate:
    sw.js parks 4xx in the failed store with the payload intact, so the work
    is preserved and visible rather than destroyed or misfiled.
    """
    owner = (request.headers.get("X-Queued-Owner") or "").strip()
    if owner and owner != str(request.state.user.id):
        return PlainTextResponse(
            "This was queued offline by a different user — sign in as them to send it.",
            status_code=409)
    return None


def claim_visit(session: Session, visit: models.Visit, expected: str) -> bool:
    """Compare-and-swap the visit's revision; True if this request won it.

    The form carries the revision the page was rendered from. A conditional
    UPDATE (WHERE id = ? AND revision = ?) is the only check that survives two
    handlers running at once: the status guard above compares values already
    read into Python, so two completions that both saw status="due" both pass
    it and the second overwrites the first tech's readings with no trace. Here
    the loser matches 0 rows and is told, rather than silently winning.

    A blank/absent `expected` means the form predates this field (an offline
    entry queued by an older build); those fall back to the status guard alone
    rather than being rejected, which would strand real field work.
    """
    if not expected:
        return True
    try:
        revision = int(expected)
    except (TypeError, ValueError):
        return False
    result = session.execute(
        update(models.Visit)
        .where(models.Visit.id == visit.id, models.Visit.revision == revision)
        .values(revision=models.Visit.revision + 1))
    if result.rowcount != 1:
        return False
    # The raw UPDATE bypassed the identity map; drop the stale in-memory value
    # so anything reading visit.revision after this sees the claimed one.
    session.refresh(visit, ["revision"])
    return True


def _f(val: str) -> float | None:
    val = (val or "").strip()
    if not val:
        return None
    # iOS/EU decimal keypads emit a comma; accept "1,5" as 1.5 so a chem dose
    # or labor override isn't silently dropped (unbilled, not stocked-out).
    val = val.replace(",", ".")
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
    # Collect-price context (Task 10) -- the ONE sanctioned money-gate
    # exception: the completer (INCLUDING a tech, who normally sees no
    # money) needs the final customer PRICE to collect on this visit.
    # Exposes PRICE ONLY -- never cost, never markup -- so it is deliberately
    # NOT wrapped in show_money.
    markup_s = models.get_setting(session, "chem_markup_percent", "0")
    try:
        markup = float(markup_s)
    except ValueError:
        markup = 0.0
    service_cents = v.rate_cents if v.rate_cents is not None else effective_rate_cents(session, v.property)
    if v.labor_override_cents is not None:
        service_cents = v.labor_override_cents
    chem_prices = {c.id: qbo.line_billable_cents(c.cost_per_unit_cents, qbo.effective_markup(c, markup))
                   for c in chems}
    tax_weekly = models.get_setting(session, "tax_weekly", "1") == "1"
    tax_chems = models.get_setting(session, "tax_chems", "1") == "1"
    # Initial preview totals (pre-JS render): sum the already-entered/prefilled
    # chem amounts at current catalog prices, so the numbers on the page are
    # correct even before the live-recompute JS runs.
    chem_cents = sum(round(chem_prices.get(cid, 0) * vc.amount) for cid, vc in existing.items())
    rate_ppm = tax.resolve_rate_ppm(session, v.property.state)
    taxable = (service_cents if tax_weekly else 0) + (chem_cents if tax_chems else 0)
    collect_tax_cents = tax.tax_cents(taxable, rate_ppm)
    collect = {
        "service_cents": service_cents,
        "chem_prices": chem_prices,
        "chem_cents": chem_cents,
        "tax_cents": collect_tax_cents,
        "total_cents": service_cents + chem_cents + collect_tax_cents,
        "rate_ppm": rate_ppm,
        "tax_labor": models.get_setting(session, "tax_labor", "1") == "1",
        "tax_chems": tax_chems,
        "tax_weekly": tax_weekly,
    }
    return templates.TemplateResponse(request, "visits/form.html", {
        "v": v, "prop": v.property, "chems": chems, "task_templates": tasks,
        "photos": photos, "rate": effective_rate_cents(session, v.property),
        "existing": existing, "show_money": request.state.user.can("view_money"),
        "collect": collect, "can_cash": request.state.user.can("mark_cash"),
        "tester_reading": water_readings.visit_reading(session, v.id),
        "tester_sources": water_readings.SOURCES})


@router.post("/visit/{visit_id}/complete")
async def complete(visit_id: int, request: Request, session: Session = Depends(get_session)):
    conflict = queued_owner_conflict(request)
    if conflict is not None:
        return conflict
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    # Optimistic-state guard against stale offline replays. The form carries
    # the visit's status as of when it was RENDERED (form_status); a completion
    # queued offline and replayed days later — after someone else completed or
    # skipped this stop — would otherwise silently overwrite the newer record
    # (readings, notes, chem lines, completed_by). A mismatch gets a 409, which
    # sw.js parks in the failed store where the queue screen explains it
    # ("already recorded by someone else") and offers retry/discard. A missing
    # field (an entry queued before this guard shipped) means the form was
    # filled against a due stop, so it defaults to "due"; a fresh interactive
    # re-complete (typo fix) renders with the current status and still matches.
    form_status = (form.get("form_status") or "due").strip()
    if v.status != form_status:
        return PlainTextResponse(
            "This stop was updated after this form was loaded — open it again to make changes.",
            status_code=409)
    if not claim_visit(session, v, (form.get("revision") or "").strip()):
        return PlainTextResponse(
            "This stop was updated by someone else while you were working — open it again.",
            status_code=409)
    was_due = v.status == "due"
    v.fc, v.ph = _f(form.get("fc")), _f(form.get("ph"))
    v.ta, v.cya, v.salt = _f(form.get("ta")), _f(form.get("cya")), _f(form.get("salt"))
    v.tasks_done = form.getlist("tasks")
    v.notes = form.get("notes", "")
    v.status = "done"
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
    # A visit completed without ever being opened keeps started_at=None
    # (unknown), by design — duration stays None and analytics must tolerate it.
    v.completed_by_id = request.state.user.id
    v.resolved = True
    if request.state.user.can("mark_cash"):
        v.paid_cash = form.get("paid_cash") == "1"
    # Freeze labor + tax rate ONCE, at first completion — same frozen-history
    # contract as the per-chemical costs below and as invoicing.py promises
    # ("Visit.rate_cents ... are read as-stored"). Re-completing to fix a typo
    # must not silently re-price the visit at a rate that moved on since.
    if v.rate_cents is None:
        v.rate_cents = effective_rate_cents(session, v.property)
    if v.tax_rate_ppm is None:
        v.tax_rate_ppm = tax.resolve_rate_ppm(session, v.property.state)
    # The override IS the amount billed for this visit, so only a money user
    # may set it. The form hides the field from everyone else, but the
    # endpoint has to enforce it too, or a hand-crafted POST re-prices the
    # visit — and a non-money tech leaves whatever is already frozen alone.
    if request.state.user.can("view_money"):
        _override = _f(form.get("labor_override_dollars"))
        if _override is not None and _override >= 0:
            override_cents = round(_override * 100)
            v.labor_override_cents = (override_cents
                                      if override_cents != (v.rate_cents or 0) else None)
        else:
            v.labor_override_cents = None
    # Preserve frozen history: a prior line's frozen unit cost must survive a
    # re-complete (e.g. fixing a pH typo) even if the catalog price moved on.
    # Only genuinely new lines freeze the current catalog cost.
    prior = {vc.chemical_id: vc.unit_cost_cents_frozen for vc in v.chemicals}
    # Stock-delta inputs: sum prior amounts per chemical BEFORE the rewrite so
    # apply_visit_delta below can compute (new - prior) and only move stock by
    # the difference — resaves net to zero, edits move the delta, removed
    # lines restore what they'd consumed.
    prior_stock: dict[int, float] = {}
    for vc in v.chemicals:
        prior_stock[vc.chemical_id] = prior_stock.get(vc.chemical_id, 0.0) + vc.amount
    for old in list(v.chemicals):
        session.delete(old)
    new_stock: dict[int, float] = {}
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
        new_stock[chem.id] = new_stock.get(chem.id, 0.0) + amount
    inventory.apply_visit_delta(session, v.id, prior_stock, new_stock)
    session.flush()
    # The tester reading the tech chose (if any) is credited as the source of
    # these numbers. Refusal (wrong pool, someone else's tester, too old)
    # never blocks the completion -- see water_readings.attach_to_visit.
    water_readings.attach_to_visit(session, v, form.get("device_reading_id"), request.state.user)
    customer = v.property.customer
    # Email only on the due→done transition. A re-complete (fixing a pH typo)
    # must not spam the homeowner with a second "visit completed"; nor may an
    # offline replay whose success response was lost mid-network — the retry
    # arrives against an already-done visit and either 409s above or, matching,
    # is a deliberate edit that still shouldn't re-send.
    if was_due and customer.email and customer.notify_email:
        business = models.get_setting(session, "business_name", "OpenPoolPro") or "OpenPoolPro"
        mailer.queue(session, customer.email, f"{business} — service visit completed",
                     service_report(session, v, business, request.state.user.name),
                     customer_id=customer.id)
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/visit/{visit_id}/skip")
async def skip(visit_id: int, request: Request, session: Session = Depends(get_session)):
    conflict = queued_owner_conflict(request)
    if conflict is not None:
        return conflict
    form = await request.form()
    v = get_or_404(session, models.Visit, visit_id)
    # Only a still-due stop may be skipped. Skipping a completed visit would
    # flip it out of every revenue/invoice/analytics query (all filter
    # status=="done") while its billed chemicals and inventory decrement stay
    # — revenue vanishes, cost doesn't. Mirrors dismiss()'s due-only guard.
    if v.status != "due":
        return RedirectResponse("/", status_code=303)
    if not claim_visit(session, v, (form.get("revision") or "").strip()):
        return PlainTextResponse(
            "This stop was updated by someone else while you were working — open it again.",
            status_code=409)
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
    if v.status == "done":
        return PlainTextResponse("This stop is already completed.", status_code=409)
    if v.status == "due":
        v.status = "skipped"
    v.completed_by_id = request.state.user.id
    v.resolved = True
    session.commit()
    return RedirectResponse("/", status_code=303)

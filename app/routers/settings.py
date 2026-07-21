import math
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chem_presets, config, mailer, models, qbo, sizes
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.main import ALLOWED_THEMES, DEFAULT_THEME, templates

router = APIRouter(dependencies=[Depends(require_admin)])

TARGET_KEYS = ("target_fc", "target_ph", "target_ta", "target_cya", "target_salt")


def _parse_count(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None  # non-int: ignore banner rather than 500 or render garbage


def _parse_checklist(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        label = line.strip()[:120]
        if label:
            out.append(label)
        if len(out) == 30:
            break
    return out


@router.get("/settings")
def settings_page(request: Request, rules_created: str | None = None,
                  mail_sent: str | None = None, mail_failed: str | None = None,
                  mail_error: str | None = None,
                  session: Session = Depends(get_session)):
    from app.routers.company import company_info
    tiers = session.scalars(select(models.RateTier).where(models.RateTier.kind == "pool").order_by(
        models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()
    spa_tiers = session.scalars(select(models.RateTier).where(models.RateTier.kind == "spa").order_by(
        models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()
    chems = session.scalars(select(models.Chemical).order_by(models.Chemical.name)).all()
    try:
        _gmarkup = float(models.get_setting(session, "chem_markup_percent", "0") or 0)
    except ValueError:
        _gmarkup = 0.0

    def _mk_fmt(v):
        return "" if v is None else ("%g" % v)

    chem_rows = [{
        "id": c.id, "name": c.name, "unit": c.unit, "role": c.role,
        "strength_pct": ("%g" % c.strength_pct), "active": c.active,
        "cost_dollars": "%.2f" % (c.cost_per_unit_cents / 100),
        "markup": _mk_fmt(c.markup_percent),
        "customer_dollars": "%.2f" % (
            qbo.line_billable_cents(c.cost_per_unit_cents, qbo.effective_markup(c, _gmarkup)) / 100),
    } for c in chems]
    tasks = session.scalars(select(models.TaskTemplate)
                            .order_by(models.TaskTemplate.sort_order)).all()
    job_templates = session.scalars(select(models.JobTemplate)
                                    .order_by(models.JobTemplate.title)).all()
    crew = session.scalars(select(models.User).where(models.User.active)
                           .order_by(models.User.role, models.User.name)).all()
    backups_dir = os.path.join(config.DATA_DIR, "backups")
    backups = sorted(os.listdir(backups_dir), reverse=True) if os.path.isdir(backups_dir) else []
    targets = {k: models.get_setting(session, k, d) for k, d in [
        ("target_fc", "4.0"), ("target_ph", "7.5"), ("target_ta", "90"),
        ("target_cya", "50"), ("target_salt", "3200")]}
    outbox = session.scalars(select(models.EmailOutbox)
                             .order_by(models.EmailOutbox.created_at.desc())
                             .limit(50)).all()
    mail_sent_n, mail_failed_n = _parse_count(mail_sent), _parse_count(mail_failed)
    return templates.TemplateResponse(request, "settings/index.html", {
        "tiers": tiers, "spa_tiers": spa_tiers, "chems": chems, "tasks": tasks, "job_templates": job_templates,
        "backups": backups,
        "crew": crew,
        "size_opts": sizes.presets_for_form(session),
        "chem_rows": chem_rows,
        "global_markup": _gmarkup,
        "app_version": config.APP_VERSION,
        "chem_presets": chem_presets.CHEM_PRESETS,
        "markup": models.get_setting(session, "chem_markup_percent", "0"),
        "active_theme": models.get_setting(session, "active_theme", DEFAULT_THEME),
        "business_name": models.get_setting(session, "business_name", ""),
        "backup_error": models.get_setting(session, "last_backup_error", ""),
        "role_values": models.ROLE_VALUES,
        "targets": targets,
        "rules_created": _parse_count(rules_created),
        "company": company_info(session),
        "smtp_configured": mailer.smtp_configured(),
        "smtp_host": config.SMTP_HOST,
        "outbox": outbox,
        "mail_result": (mail_sent_n, mail_failed_n) if mail_sent_n is not None
                       and mail_failed_n is not None else None,
        "mail_error": mail_error})


@router.post("/settings/maintenance/run-now")
def run_maintenance_now(session: Session = Depends(get_session)):
    from app.maintenance import run_maintenance_rules
    from app.routers.today import business_today
    created = run_maintenance_rules(session, business_today())
    return RedirectResponse(f"/settings?rules_created={created}", status_code=303)


@router.post("/settings/targets")
async def save_targets(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for key in TARGET_KEYS:
        raw = (form.get(key) or "").strip()
        try:
            v = float(raw)
        except ValueError:
            continue  # invalid keeps the previously stored value
        if not math.isfinite(v):
            continue  # inf/nan keeps the previously stored value
        models.set_setting(session, key, raw)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


async def _save_tiers_of_kind(request: Request, session: Session, kind: str) -> None:
    form = await request.form()
    rows = zip(form.getlist("tier_id"), form.getlist("max_gallons"),
               form.getlist("rate_dollars"), form.getlist("label"))
    changed = 0
    for tid, max_g, rate, label in rows:
        tier = None
        if tid:
            try:
                tier = session.get(models.RateTier, int(tid))
            except ValueError:
                continue
            if tier is None or tier.kind != kind:   # kind guard: never touch a tier of the other kind
                continue
        if not rate.strip():
            if tier is not None:
                session.delete(tier)
                changed += 1
            continue
        try:
            rate_cents = round(float(rate) * 100)
            max_gallons = int(max_g) if max_g.strip() else None
        except ValueError:
            continue
        if tier is None:
            tier = models.RateTier(rate_cents=0, kind=kind)
            changed += 1
        else:
            before = (tier.max_gallons, tier.rate_cents, tier.label)
            if (max_gallons, rate_cents, label) != before:
                changed += 1
        tier.max_gallons, tier.rate_cents, tier.label = max_gallons, rate_cents, label
        session.add(tier)
    if changed:
        models.audit(session, request.state.user.id, "rates.change",
                     detail=f"{changed} {kind} rate tier(s) changed")
    session.commit()


@router.post("/settings/tiers")
async def save_tiers(request: Request, session: Session = Depends(get_session)):
    await _save_tiers_of_kind(request, session, "pool")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/spa_tiers")
async def save_spa_tiers(request: Request, session: Session = Depends(get_session)):
    await _save_tiers_of_kind(request, session, "spa")
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/job_templates/{tid}/prices")
def template_prices_page(request: Request, tid: int, session: Session = Depends(get_session)):
    t = get_or_404(session, models.JobTemplate, tid)
    return templates.TemplateResponse(request, "settings/template_prices.html",
        {"t": t, "size_opts": sizes.presets_for_form(session)})


@router.post("/settings/job_templates/{tid}/prices")
async def save_template_prices(request: Request, tid: int,
                               session: Session = Depends(get_session)):
    t = get_or_404(session, models.JobTemplate, tid)
    form = await request.form()
    rows = zip(form.getlist("price_id"), form.getlist("label"),
               form.getlist("max_gallons"), form.getlist("price_dollars"))
    for pid, label, max_g, price in rows:
        row = None
        if pid:
            try:
                row = session.get(models.JobTemplatePrice, int(pid))
            except ValueError:
                continue
            if row is None or row.job_template_id != t.id:
                continue
        if not (price or "").strip():
            if row is not None:
                session.delete(row)  # blank price removes the row
            continue
        try:
            price_cents = round(float(price) * 100)
            max_gallons = int(max_g) if (max_g or "").strip() else None
        except ValueError:
            continue
        if row is None:
            row = models.JobTemplatePrice(job_template_id=t.id)
        row.label, row.max_gallons, row.price_cents = label, max_gallons, price_cents
        session.add(row)
    session.commit()
    return RedirectResponse(f"/settings/job_templates/{tid}/prices", status_code=303)


def _parse_markup(raw: str) -> float | None:
    """Per-item markup %: blank/garbage -> None (use the global markup)."""
    raw = (raw or "").strip().rstrip("%").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if (math.isfinite(v) and v >= 0) else None


@router.post("/settings/chemicals")
async def save_chemicals(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    chem_ids = form.getlist("chem_id")
    names = form.getlist("name")
    units = form.getlist("unit")
    costs = form.getlist("cost_dollars")
    roles = form.getlist("role")
    strengths = form.getlist("strength_pct")
    markups = form.getlist("markup")

    for i, (cid, name, unit, cost) in enumerate(zip(chem_ids, names, units, costs)):
        role = roles[i] if i < len(roles) else "other"
        strength = strengths[i] if i < len(strengths) else ""
        markup_raw = markups[i] if i < len(markups) else ""

        if cid:
            try:
                c = session.get(models.Chemical, int(cid))
            except ValueError:
                continue
            if c is None:
                continue
            if not name.strip():
                c.active = False
                continue
            try:
                cost_cents = round(float(cost or 0) * 100)
            except ValueError:
                continue
            c.name, c.unit = name, unit
            c.cost_per_unit_cents = cost_cents
            c.role = role if role in models.ROLE_VALUES else "other"
            try:
                new_strength = float(strength) if strength.strip() else c.strength_pct
            except ValueError:
                new_strength = c.strength_pct
            if math.isfinite(new_strength) and new_strength > 0:
                c.strength_pct = new_strength
            c.markup_percent = _parse_markup(markup_raw)
            c.active = form.get(f"active_{cid}") == "on"
        elif name.strip():
            try:
                cost_cents = round(float(cost or 0) * 100)
            except ValueError:
                continue
            chem_role = role if role in models.ROLE_VALUES else "other"
            try:
                strength_pct = float(strength) if strength.strip() else 100.0
            except ValueError:
                strength_pct = 100.0
            if not (math.isfinite(strength_pct) and strength_pct > 0):
                strength_pct = 100.0
            session.add(models.Chemical(name=name, unit=unit,
                                        cost_per_unit_cents=cost_cents,
                                        role=chem_role, strength_pct=strength_pct,
                                        markup_percent=_parse_markup(markup_raw)))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/chemicals/add-from-library")
async def add_chemical_from_library(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    preset = chem_presets.by_name.get((form.get("preset_name") or "").strip())
    if preset is not None:
        exists = session.scalars(select(models.Chemical).where(
            models.Chemical.name == preset["name"])).first()
        if exists is None:
            session.add(models.Chemical(
                name=preset["name"], unit=preset["unit"], cost_per_unit_cents=0,
                role=preset["role"], strength_pct=preset["strength_pct"]))
            session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/tasks")
async def save_tasks(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for i, (tid, label) in enumerate(zip(form.getlist("task_id"), form.getlist("label"))):
        if tid:
            try:
                t = session.get(models.TaskTemplate, int(tid))
            except ValueError:
                continue
            if t is None:
                continue
            if not label.strip():
                t.active = False
            else:
                t.label, t.sort_order = label, i
        elif label.strip():
            session.add(models.TaskTemplate(label=label, sort_order=i))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/job_templates")
async def save_job_templates(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    ids = form.getlist("template_id")
    titles = form.getlist("title")
    descriptions = form.getlist("description")
    job_types = form.getlist("job_type")
    labors = form.getlist("labor_dollars")
    tasks_texts = form.getlist("tasks_text")

    for i, (tid, title) in enumerate(zip(ids, titles)):
        description = descriptions[i] if i < len(descriptions) else ""
        job_type = job_types[i] if i < len(job_types) else "service"
        job_type = job_type if job_type in ("service", "repair") else "service"
        labor = labors[i] if i < len(labors) else ""

        if tid:
            try:
                t = session.get(models.JobTemplate, int(tid))
            except ValueError:
                continue
            if t is None:
                continue
            if not title.strip():
                session.delete(t)  # blank title deletes -- JobTemplate has no active flag
                continue
            try:
                labor_cents = round(float(labor or 0) * 100)
            except ValueError:
                continue
            t.title, t.description, t.job_type = title, description, job_type
            t.labor_cents = labor_cents
            t.tasks = [models.JobTemplateTask(label=lbl, position=k)
                       for k, lbl in enumerate(_parse_checklist(
                           tasks_texts[i] if i < len(tasks_texts) else ""))]
        elif title.strip():
            try:
                labor_cents = round(float(labor or 0) * 100)
            except ValueError:
                continue
            tmpl = models.JobTemplate(title=title, description=description,
                                      job_type=job_type, labor_cents=labor_cents)
            tmpl.tasks = [models.JobTemplateTask(label=lbl, position=k)
                          for k, lbl in enumerate(_parse_checklist(
                              tasks_texts[i] if i < len(tasks_texts) else ""))]
            session.add(tmpl)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/appearance")
async def save_appearance(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    theme = (form.get("active_theme") or "").strip()
    # Only persist a recognized theme; anything else (garbage/tampering) keeps
    # whatever was previously stored rather than saving an invalid value that
    # the middleware would just fall back on -- mirrors save_general/save_targets.
    if theme in ALLOWED_THEMES:
        models.set_setting(session, "active_theme", theme)
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/general")
async def save_general(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    # Update only the fields actually submitted, so the markup (Pricing tab) and the
    # display name (Business tab) can post from separate forms without clobbering
    # each other.
    if "chem_markup_percent" in form:
        raw = (form.get("chem_markup_percent", "0") or "0").strip()
        if raw.endswith("%"):
            raw = raw[:-1].strip()
        try:
            v = float(raw)
            if not math.isfinite(v):
                raise ValueError("inf/nan markup")
            markup = raw
        except ValueError:
            # Garbage (or inf/nan) input: keep whatever was previously stored
            # rather than overwriting it with something that will 500 the Money page.
            markup = models.get_setting(session, "chem_markup_percent", "0")
        models.set_setting(session, "chem_markup_percent", markup)
    if "business_name" in form:
        models.set_setting(session, "business_name", form.get("business_name", ""))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/backups/{filename}")
def download_backup(filename: str):
    if "/" in filename or ".." in filename:
        return HTMLResponse("nope", status_code=400)
    path = os.path.join(config.DATA_DIR, "backups", filename)
    if not os.path.isfile(path):
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path, filename=filename)


@router.post("/settings/mail/test")
async def send_test_email(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    to_addr = (form.get("to_addr") or "").strip()
    if not to_addr or "@" not in to_addr:
        return RedirectResponse("/settings?mail_error=1", status_code=303)
    mailer.queue(session, to_addr, "OpenPoolPro test email",
                "This is a test email from OpenPoolPro.")
    session.commit()
    sent, failed = mailer.flush_outbox(session)
    session.commit()
    return RedirectResponse(f"/settings?mail_sent={sent}&mail_failed={failed}", status_code=303)


@router.post("/settings/mail/flush")
def flush_mail_now(session: Session = Depends(get_session)):
    sent, failed = mailer.flush_outbox(session)
    session.commit()
    return RedirectResponse(f"/settings?mail_sent={sent}&mail_failed={failed}", status_code=303)

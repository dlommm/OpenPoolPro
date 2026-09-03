import math
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chem_presets, config, mailer, models, qbo, rates, receivables, sizes, tax
from app.company import company_info
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.templating import ALLOWED_THEMES, DEFAULT_THEME, templates
from app.timefmt import business_today

router = APIRouter(dependencies=[Depends(require_admin)])

TARGET_KEYS = ("target_fc", "target_ph", "target_ta", "target_cya", "target_salt")

# Older than this and the nightly snapshot is presumed to have stopped running.
BACKUP_STALE_HOURS = 36


def _backup_is_stale(session) -> bool:
    """True when the last successful backup is old (or never happened).

    A backup that silently stops is indistinguishable from one that works until
    the day it is needed, so the absence of a recent success has to be visible
    on its own — not just the presence of an error.
    """
    from datetime import datetime, timedelta
    stamp = models.get_setting(session, "last_backup_at", "")
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    return (now - when) > timedelta(hours=BACKUP_STALE_HOURS)


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
    tiers = session.scalars(select(models.RateTier).where(models.RateTier.kind == "pool").order_by(
        models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()
    spa_tiers = session.scalars(select(models.RateTier).where(models.RateTier.kind == "spa").order_by(
        models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()
    contract_rates = session.scalars(select(models.ContractRate).order_by(
        models.ContractRate.max_gallons.is_(None), models.ContractRate.max_gallons)).all()
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
        "tiers": tiers, "spa_tiers": spa_tiers, "contract_rates": contract_rates,
        "chems": chems, "tasks": tasks, "job_templates": job_templates,
        "backups": backups,
        "crew": crew,
        "size_opts": sizes.presets_for_form(session),
        "chem_rows": chem_rows,
        "global_markup": _gmarkup,
        "app_version": config.APP_VERSION,
        "chem_presets": chem_presets.CHEM_PRESETS,
        "markup": models.get_setting(session, "chem_markup_percent", "0"),
        "labor_rate_dollars": "%.2f" % (rates.default_labor_rate_cents(session) / 100),
        "active_theme": models.get_setting(session, "active_theme", DEFAULT_THEME),
        "business_name": models.get_setting(session, "business_name", ""),
        "invoice_net_days": receivables.default_net_days(session),
        "backup_error": models.get_setting(session, "last_backup_error", ""),
        # Off-box sync failing is the quiet one: the local snapshot succeeds, so
        # nothing looks wrong while the only copy that survives a dead disk
        # silently stops being made.
        "backup_sync_error": models.get_setting(session, "last_backup_sync_error", ""),
        "backup_at": models.get_setting(session, "last_backup_at", ""),
        "backup_stale": _backup_is_stale(session),
        "role_values": models.ROLE_VALUES,
        "targets": targets,
        "rules_created": _parse_count(rules_created),
        "company": company_info(session),
        "smtp_configured": mailer.smtp_configured(session),
        "smtp": {**mailer.smtp_settings(session, force_password=True),
                 "pass_set": bool(models.get_setting(session, "smtp_pass", config.SMTP_PASS))},
        "smtp_active_host": mailer.smtp_settings(session)["host"],
        **_o365_ctx(request, session),
        "geoapify_configured": bool(models.get_setting(session, "geoapify_key", "")),
        "public_base_url": models.get_setting(session, "public_base_url", ""),
        "conn_check": {
            "client_ip": request.client.host if request.client else "(unknown)",
            "public_ip": (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                          or (request.client.host if request.client else "")),
            "xff": request.headers.get("x-forwarded-for", ""),
            "x_real_ip": request.headers.get("x-real-ip", ""),
            "forwarded_proto": request.headers.get("x-forwarded-proto", ""),
            "proxy_auth_enabled": bool(config.PROXY_AUTH_SECRET),
            "proxy_auth_header": config.PROXY_AUTH_HEADER,
            "proxy_auth_present": bool(request.headers.get(config.PROXY_AUTH_HEADER)),
        },
        "outbox": outbox,
        "mail_result": (mail_sent_n, mail_failed_n) if mail_sent_n is not None
                       and mail_failed_n is not None else None,
        "mail_error": mail_error,
        "tax_enabled": models.get_setting(session, "sales_tax_enabled", "0") == "1",
        "tax_default_pct": tax.format_ppm(int(models.get_setting(session, "sales_tax_default_ppm", "0") or 0)),
        "tax_labor": models.get_setting(session, "tax_labor", "1") == "1",
        "tax_chems": models.get_setting(session, "tax_chems", "1") == "1",
        "tax_weekly": models.get_setting(session, "tax_weekly", "1") == "1",
        "tax_rates": [(r.state, tax.format_ppm(r.rate_ppm), r.overridden)
                      for r in session.scalars(select(models.TaxRate).order_by(models.TaxRate.state)).all()],
        **_contract_tab_ctx(session)})


def _contract_tab_ctx(session) -> dict:
    """Context for the Settings 'Contract' tab: the stored template, the token
    palette, and a preview rendered against the sample context."""
    from app import contract_tokens
    from app.contract_defaults import get_template
    from app.routers.contract_template import _preview_html, sample_context
    tpl = get_template(session)
    return {
        "contract_template": tpl,
        "contract_tokens": contract_tokens.TOKENS,
        "contract_preview_html": _preview_html(tpl, sample_context()),
    }


@router.post("/settings/maintenance/run-now")
def run_maintenance_now(session: Session = Depends(get_session)):
    from app.maintenance import run_maintenance_rules
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


# (dollar field name, cents field name) pairs, in the order the settings grid renders them.
CONTRACT_RATE_DOLLAR_FIELDS = tuple(
    (cents_field.removesuffix("_cents") + "_dollars", cents_field)
    for cents_field in rates.CONTRACT_RATE_FIELDS
)


@router.post("/settings/contract_rates")
async def save_contract_rates(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    ids = form.getlist("contract_rate_id")
    max_gs = form.getlist("max_gallons")
    labels = form.getlist("label")
    dollar_lists = {dollar: form.getlist(dollar) for dollar, _cents in CONTRACT_RATE_DOLLAR_FIELDS}

    def _at(lst: list[str], i: int) -> str:
        return lst[i] if i < len(lst) else ""

    changed = 0
    for i, (rid, max_g) in enumerate(zip(ids, max_gs)):
        label = _at(labels, i)
        row = None
        if rid:
            try:
                row = session.get(models.ContractRate, int(rid))
            except ValueError:
                continue
            if row is None:
                continue
        weekly_raw = _at(dollar_lists["weekly_dollars"], i)
        if not weekly_raw.strip():
            if row is not None:
                session.delete(row)
                changed += 1
            continue
        try:
            max_gallons = int(max_g) if max_g.strip() else None
            cents = {}
            for dollar_field, cents_field in CONTRACT_RATE_DOLLAR_FIELDS:
                raw = _at(dollar_lists[dollar_field], i).strip()
                cents[cents_field] = round(float(raw) * 100) if raw else 0
        except ValueError:
            continue  # garbage anywhere in this row: skip it rather than 500 or half-save
        if row is None:
            row = models.ContractRate()
            changed += 1
        else:
            before = (row.max_gallons, row.label,
                     tuple(getattr(row, cents_field) for _, cents_field in CONTRACT_RATE_DOLLAR_FIELDS))
            after = (max_gallons, label,
                    tuple(cents[cents_field] for _, cents_field in CONTRACT_RATE_DOLLAR_FIELDS))
            if before != after:
                changed += 1
        row.max_gallons, row.label = max_gallons, label
        for _, cents_field in CONTRACT_RATE_DOLLAR_FIELDS:
            setattr(row, cents_field, cents[cents_field])
        session.add(row)
    if changed:
        models.audit(session, request.state.user.id, "rates.change",
                     detail=f"{changed} contract rate(s) changed")
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/labor_rate")
async def save_labor_rate(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    raw = (form.get("labor_rate_dollars") or "").strip()
    try:
        dollars = float(raw)
    except ValueError:
        dollars = None
    if dollars is not None and math.isfinite(dollars) and dollars >= 0:
        models.set_setting(session, "labor_rate_cents", str(round(dollars * 100)))
        session.commit()
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
               form.getlist("max_gallons"), form.getlist("hours"))
    for pid, label, max_g, hours in rows:
        row = None
        if pid:
            try:
                row = session.get(models.JobTemplatePrice, int(pid))
            except ValueError:
                continue
            if row is None or row.job_template_id != t.id:
                continue
        if not (hours or "").strip():
            if row is not None:
                session.delete(row)  # blank hours removes the row
            continue
        try:
            hours_val = float(hours)
            max_gallons = int(max_g) if (max_g or "").strip() else None
        except ValueError:
            continue
        if row is None:
            row = models.JobTemplatePrice(job_template_id=t.id)
        row.label, row.max_gallons, row.hours = label, max_gallons, hours_val
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
    est_hours_list = form.getlist("est_hours")
    tasks_texts = form.getlist("tasks_text")

    for i, (tid, title) in enumerate(zip(ids, titles)):
        description = descriptions[i] if i < len(descriptions) else ""
        job_type = job_types[i] if i < len(job_types) else "service"
        job_type = job_type if job_type in ("service", "repair") else "service"
        labor = labors[i] if i < len(labors) else ""
        est_hours_raw = est_hours_list[i] if i < len(est_hours_list) else ""
        try:
            est_hours = float(est_hours_raw) if est_hours_raw.strip() else 1.0
        except ValueError:
            est_hours = 1.0
        if not (math.isfinite(est_hours) and est_hours >= 0):
            est_hours = 1.0  # tolerant: garbage/negative/inf/nan falls back to 1.0

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
            t.est_hours = est_hours
            t.tasks = [models.JobTemplateTask(label=lbl, position=k)
                       for k, lbl in enumerate(_parse_checklist(
                           tasks_texts[i] if i < len(tasks_texts) else ""))]
        elif title.strip():
            try:
                labor_cents = round(float(labor or 0) * 100)
            except ValueError:
                continue
            tmpl = models.JobTemplate(title=title, description=description,
                                      job_type=job_type, labor_cents=labor_cents,
                                      est_hours=est_hours)
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
    if "invoice_net_days" in form:
        # Garbage keeps the stored value rather than writing something that
        # would push every due date somewhere nonsensical in the aging report.
        raw = (form.get("invoice_net_days", "") or "").strip()
        try:
            days = int(raw)
            if days >= 0:
                models.set_setting(session, "invoice_net_days", str(days))
        except ValueError:
            pass
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


@router.post("/settings/smtp")
async def save_smtp(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for key in ("smtp_host", "smtp_user", "smtp_from"):
        models.set_setting(session, key, (form.get(key) or "").strip())
    port = (form.get("smtp_port") or "").strip()
    if port.isdigit():
        models.set_setting(session, "smtp_port", port)
    models.set_setting(session, "smtp_starttls", "1" if form.get("smtp_starttls") == "on" else "0")
    pw = form.get("smtp_pass")  # write-only: only overwrite when a value is supplied
    if pw is not None and pw.strip():
        models.set_setting(session, "smtp_pass", pw)
    # Saving the password form is an explicit choice of password auth.
    if form.get("use_password_auth") == "on":
        models.set_setting(session, "smtp_auth_mode", "password")
    session.commit()
    return RedirectResponse("/settings", status_code=303)


def _o365_ctx(request: Request, session: Session) -> dict:
    """Context for the Office 365 status fragment (and the settings page)."""
    from app import msoauth
    pending = getattr(request.app.state, "msoauth_pending", None)
    return {
        "o365_client_id": models.get_setting(session, "smtp_oauth_client_id", ""),
        "o365_tenant": models.get_setting(session, "smtp_oauth_tenant", ""),
        "o365_account": models.get_setting(session, "smtp_oauth_account", ""),
        "o365_connected": msoauth.connected(session),
        "o365_active": models.get_setting(session, "smtp_auth_mode", "password") == "oauth365",
        "o365_pending": pending,
    }


@router.post("/settings/smtp/oauth")
async def save_smtp_oauth(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    models.set_setting(session, "smtp_oauth_client_id",
                       (form.get("client_id") or "").strip())
    models.set_setting(session, "smtp_oauth_tenant",
                       (form.get("tenant") or "").strip())
    session.commit()
    return RedirectResponse("/settings#system", status_code=303)


@router.post("/settings/smtp/oauth/start")
def start_smtp_oauth(request: Request, session: Session = Depends(get_session)):
    from app import msoauth
    client_id = models.get_setting(session, "smtp_oauth_client_id", "")
    tenant = models.get_setting(session, "smtp_oauth_tenant", "")
    ctx = _o365_ctx(request, session)
    if not client_id:
        ctx["o365_error"] = "Save an Application (client) ID first."
    else:
        try:
            request.app.state.msoauth_pending = msoauth.start_device_flow(client_id, tenant)
            ctx["o365_pending"] = request.app.state.msoauth_pending
        except msoauth.MsAuthError as e:
            ctx["o365_error"] = str(e)
        except Exception:
            ctx["o365_error"] = "Could not reach Microsoft — check the network and try again."
    return templates.TemplateResponse(request, "settings/_o365_status.html", ctx)


@router.get("/settings/smtp/oauth/poll")
def poll_smtp_oauth(request: Request, session: Session = Depends(get_session)):
    from app import msoauth
    pending = getattr(request.app.state, "msoauth_pending", None)
    ctx = _o365_ctx(request, session)
    if pending is not None:
        try:
            account = msoauth.poll_device_flow(session, pending)
            if account is not None:
                request.app.state.msoauth_pending = None
                models.audit(session, request.state.user.id, "settings.smtp_oauth",
                             detail=f"connected {account}")
                session.commit()
                ctx = _o365_ctx(request, session)
        except msoauth.MsAuthError as e:
            request.app.state.msoauth_pending = None
            ctx = _o365_ctx(request, session)
            ctx["o365_error"] = str(e)
        except Exception:
            ctx["o365_error"] = "Could not reach Microsoft — still trying…"
    return templates.TemplateResponse(request, "settings/_o365_status.html", ctx)


@router.post("/settings/smtp/oauth/disconnect")
def disconnect_smtp_oauth(request: Request, session: Session = Depends(get_session)):
    from app import msoauth
    msoauth.disconnect(session)
    models.audit(session, request.state.user.id, "settings.smtp_oauth",
                 detail="disconnected")
    session.commit()
    return RedirectResponse("/settings#system", status_code=303)


@router.post("/settings/geoapify")
async def save_geoapify(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    gk = form.get("geoapify_key")  # write-only: only overwrite when supplied
    if gk is not None and gk.strip():
        models.set_setting(session, "geoapify_key", gk.strip())
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/public-url")
async def save_public_url(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    raw = (form.get("public_base_url") or "").strip().rstrip("/")
    # Blank clears the override (customer links fall back to the request
    # origin); anything set must be an absolute http(s) origin.
    if raw and not raw.startswith(("http://", "https://")):
        return RedirectResponse("/settings#system", status_code=303)
    models.set_setting(session, "public_base_url", raw)
    session.commit()
    return RedirectResponse("/settings#system", status_code=303)


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


@router.post("/settings/tax")
async def save_tax(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    models.set_setting(session, "sales_tax_enabled", "1" if form.get("sales_tax_enabled") == "on" else "0")
    models.set_setting(session, "sales_tax_default_ppm", str(tax.parse_percent(form.get("sales_tax_default_pct", "0"))))
    for key in ("tax_labor", "tax_chems", "tax_weekly"):
        models.set_setting(session, key, "1" if form.get(key) == "on" else "0")
    session.commit()
    return RedirectResponse("/settings#tax", status_code=303)


@router.post("/settings/tax/state")
async def save_tax_state(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    state = (form.get("state") or "").strip().upper()[:2]
    row = session.get(models.TaxRate, state)
    if row is not None:
        row.rate_ppm = tax.parse_percent(form.get("pct", "0"))
        row.overridden = True
        session.commit()
    return RedirectResponse("/settings#tax", status_code=303)


@router.post("/settings/tax/state/{state}/reset")
def reset_tax_state(state: str, session: Session = Depends(get_session)):
    from app.tax_data import STATE_SALES_TAX
    row = session.get(models.TaxRate, state.upper())
    if row is not None and state.upper() in STATE_SALES_TAX:
        row.rate_ppm = tax.parse_percent(str(STATE_SALES_TAX[state.upper()]))
        row.overridden = False
        session.commit()
    return RedirectResponse("/settings#tax", status_code=303)

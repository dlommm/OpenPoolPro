import math
import os
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    chem_presets,
    chem_targets,
    config,
    mailer,
    models,
    palettes,
    qbo,
    rates,
    receivables,
    sizes,
    tax,
    templating,
)
from app.company import company_info
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.templating import templates
from app.timefmt import business_today

router = APIRouter(dependencies=[Depends(require_admin)])


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
    appearance = templating.ui_settings(session)
    palette_cards = [{"key": p.key, "name": p.name, "desc": p.desc, "on": appearance["palette"] == p.key,
                      "v": palettes.preview(p.key, p.base)} for p in palettes.PALETTES]
    custom = appearance["palette_custom"]
    palette_cards.append({"key": "custom", "name": "Your colour",
                          "desc": "Any brand colour; contrast is corrected for you.",
                          "on": appearance["palette"] == "custom", "v": palettes.preview("custom", custom)})
    fixed = palettes.derive(custom, "light")["--a"]
    palette_custom_fix = fixed if fixed != custom else ""
    chem_t, chem_b = chem_targets.current(session)
    rejected = set((request.query_params.get("chem_rejected") or "").split(","))
    target_rows = [{"spec": sp, "target": chem_t[sp.key], "band": chem_b[sp.key],
                    "rejected": sp.key in rejected} for sp in chem_targets.SPECS]
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
        "appearance": appearance,
        "palette_cards": palette_cards,
        "palette_custom_fix": palette_custom_fix,
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
        "chem_targets": target_rows,
        "chem_saved": request.query_params.get("chem"),
        "rules_created": _parse_count(rules_created),
        "company": company_info(session),
        "smtp_configured": mailer.smtp_configured(session),
        "smtp": {**mailer.smtp_settings(session, force_password=True),
                 "pass_set": bool(models.get_setting(session, "smtp_pass", config.SMTP_PASS))},
        "smtp_active_host": mailer.smtp_settings(session)["host"],
        **_o365_ctx(request, session),
        "geoapify_configured": bool(models.get_setting(session, "geoapify_key", "")),
        "public_base_url": models.get_setting(session, "public_base_url", ""),
        "require_2fa": models.get_setting(session, "require_2fa", "0") == "1",
        **_qbo_ctx(session),
        "qbo_items": _qbo_items_maybe(request, session),
        "qbo_accounts": _qbo_accounts_maybe(request),
        "qbo_default_item_names": qbo_api_names(),
        # Flags/counts only — never free text from the URL. The error message
        # itself comes from the qbo_last_error setting (server-written), so a
        # crafted link can't render attacker copy inside the admin UI.
        "qbo_result": {
            "qbo_ok": bool(request.query_params.get("qbo_ok")),
            "qbo_error": bool(request.query_params.get("qbo_error")),
            "qbo_synced": _parse_count(request.query_params.get("qbo_synced")),
            "qbo_failed": _parse_count(request.query_params.get("qbo_failed")),
            "qbo_items_created": _parse_count(request.query_params.get("qbo_items_created")),
            "qbo_items_adopted": _parse_count(request.query_params.get("qbo_items_adopted")),
        },
        "staff_without_2fa": session.scalars(select(models.User).where(
            models.User.active, models.User.totp_enabled.is_(False))).all(),
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
    palette, a preview rendered against the sample context, and the editable
    e-sign message templates."""
    from app import contract_tokens, sign_messages
    from app.contract_defaults import get_template
    from app.routers.contract_template import _preview_html, sample_context
    tpl = get_template(session)
    return {
        "contract_template": tpl,
        "contract_tokens": contract_tokens.TOKENS,
        "contract_preview_html": _preview_html(tpl, sample_context()),
        "sign_messages": {k: sign_messages.get_message(session, k)
                          for k in sign_messages.DEFAULTS},
        "sign_message_tokens": sign_messages.TOKENS,
    }


@router.post("/settings/maintenance/run-now")
def run_maintenance_now(session: Session = Depends(get_session)):
    from app.maintenance import run_maintenance_rules
    created = run_maintenance_rules(session, business_today())
    return RedirectResponse(f"/settings?rules_created={created}", status_code=303)


@router.post("/settings/targets")
async def save_targets(request: Request, session: Session = Depends(get_session)):
    """Target and band per reading, saved as a pair: a pair that fails the
    limits in app/chem_targets.py keeps what was stored, so a typo can't
    switch dosing off. A field left out of the form keeps its stored value."""
    form = await request.form()
    if form.get("reset"):
        for spec in chem_targets.SPECS:
            models.set_setting(session, f"target_{spec.key}", f"{spec.target:g}")
            models.set_setting(session, f"band_{spec.key}", f"{spec.band:g}")
        session.commit()
        return RedirectResponse("/settings?chem=reset#chemistry", status_code=303)
    targets, bands = chem_targets.current(session)
    rejected = []
    for spec in chem_targets.SPECS:
        pair, raws = {}, {}
        for prefix, stored in (("target", targets), ("band", bands)):
            raw = (form.get(f"{prefix}_{spec.key}") or "").strip()
            raws[prefix] = raw or f"{stored[spec.key]:g}"
            try:
                pair[prefix] = float(raws[prefix])
            except ValueError:
                pair[prefix] = math.nan
        if chem_targets.problem(spec.key, pair["target"], pair["band"]):
            rejected.append(spec.key)
            continue  # invalid keeps the previously stored pair
        models.set_setting(session, f"target_{spec.key}", raws["target"])
        models.set_setting(session, f"band_{spec.key}", raws["band"])
    session.commit()
    q = f"chem_rejected={','.join(rejected)}" if rejected else "chem=saved"
    return RedirectResponse(f"/settings?{q}#chemistry", status_code=303)


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
    # Only persist recognized values; anything else (garbage/tampering) keeps
    # whatever was previously stored -- mirrors save_general/save_targets.
    for key, allowed in (("nav_layout", templating.NAV_LAYOUTS),
                         ("header_style", templating.HEADER_STYLES)):
        value = (form.get(key) or "").strip()
        if value in allowed:
            models.set_setting(session, key, value)
    pal = (form.get("palette") or "").strip()
    if pal in palettes.BY_KEY or pal == "custom":
        models.set_setting(session, "palette", pal)
    custom = palettes.clean_hex(form.get("palette_custom"))
    if custom:
        models.set_setting(session, "palette_custom", custom)
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


def _qbo_redirect_uri(request: Request, session: Session) -> str:
    """The OAuth callback URL — must match what's registered in the Intuit app.
    Uses public_base_url when set (staff may browse by LAN address), else the
    request origin."""
    base = models.get_setting(session, "public_base_url", "").strip() or str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/settings/qbo/callback"


def _qbo_items_maybe(request: Request, session):
    """QBO items for the mapping dropdowns. Populated by the POST /qbo/items-load
    route (which stashes them in the session), never by a GET query param: a
    page render must never make an outbound call, or a crafted link could make
    an admin's browser drive traffic to Intuit."""
    return request.session.pop("qbo_items", None)


def _qbo_accounts_maybe(request: Request):
    """Income accounts for the item-provisioning dropdown. Same POST-only rule
    as _qbo_items_maybe: a page render never makes an outbound call."""
    return request.session.pop("qbo_accounts", None)


def _qbo_fail(request: Request, session: Session, message: str) -> RedirectResponse:
    """Record an error for display and redirect. The text is stored server-side
    rather than echoed through the URL — a reflected `?qbo_error=<anything>`
    would let an attacker render arbitrary copy inside the real admin UI
    ('QuickBooks needs re-authorization, call 1-800-...')."""
    models.set_setting(session, "qbo_last_error", message[:300])
    session.commit()
    return RedirectResponse("/settings?qbo_error=1#system", status_code=303)


def qbo_api_names() -> dict:
    from app import qbo_api
    return qbo_api.DEFAULT_ITEM_NAMES


def _qbo_ctx(session) -> dict:
    from app import qbo_api
    return {
        "qbo_client_id": models.get_setting(session, "qbo_client_id", ""),
        "qbo_secret_set": bool(models.get_setting(session, "qbo_client_secret", "")),
        "qbo_environment": models.get_setting(session, "qbo_environment", "sandbox"),
        "qbo_configured": qbo_api.configured(session),
        "qbo_connected": qbo_api.connected(session),
        "qbo_company": models.get_setting(session, "qbo_company_name", ""),
        "qbo_item_service": models.get_setting(session, "qbo_item_service_id", ""),
        "qbo_item_chem": models.get_setting(session, "qbo_item_chem_id", ""),
        "qbo_last_error": models.get_setting(session, "qbo_last_error", ""),
    }


@router.post("/settings/qbo/config")
async def save_qbo_config(request: Request, session: Session = Depends(get_session)):
    from app import qbo_api
    form = await request.form()
    with qbo_api._refresh_lock:
        old_config = qbo_api._cfg(session)
        models.set_setting(session, "qbo_client_id", (form.get("client_id") or "").strip())
        env = "production" if form.get("environment") == "production" else "sandbox"
        models.set_setting(session, "qbo_environment", env)
        secret = form.get("client_secret")  # write-only, blank keeps the stored one
        if secret is not None and secret.strip():
            models.set_setting(session, "qbo_client_secret", secret.strip())
        new_config = qbo_api._cfg(session)
        if any(old_config[key] != new_config[key] for key in ("client_id", "client_secret", "environment")):
            for key in ("qbo_refresh_token", "qbo_realm_id"):
                models.set_setting(session, key, "")
            qbo_api.invalidate_mapping_settings(session)
            request.session.pop("qbo_oauth_state", None)
            request.session.pop("qbo_code_verifier", None)
            request.session.pop("qbo_items", None)
        session.commit()
        # New credentials or a sandbox<->production flip must not keep using a
        # token minted for the old config (valid for up to an hour).
        from app import qbo_api
        qbo_api.clear_cache()
    return RedirectResponse("/settings#system", status_code=303)


# POST, not GET: starting a connect mints a state nonce into the session, and
# SameSite=Lax lets a cross-site top-level GET navigation carry the admin's
# cookie — so a GET version could be driven by a link on a phishing page.
@router.post("/settings/qbo/connect")
def qbo_connect(request: Request, session: Session = Depends(get_session)):
    from app import qbo_api
    try:
        state = secrets.token_urlsafe(24)
        verifier = qbo_api.new_code_verifier()
        url = qbo_api.authorize_url(session, _qbo_redirect_uri(request, session),
                                    state, verifier)
    except qbo_api.QboError as e:
        return _qbo_fail(request, session, str(e))
    # Stored only after authorize_url succeeded, so a misconfigured attempt
    # doesn't leave a dangling nonce.
    request.session["qbo_oauth_state"] = state
    request.session["qbo_code_verifier"] = verifier
    return RedirectResponse(url, status_code=303)


@router.get("/settings/qbo/callback")
def qbo_callback(request: Request, code: str = "", state: str = "", realmId: str = "",
                 error: str = "", session: Session = Depends(get_session)):
    from app import qbo_api
    # Read (don't consume) first: a forged/cancelled callback must not eat a
    # pending nonce, or an attacker could repeatedly navigate the admin to
    # ?error=1 and make a genuine connect impossible to finish.
    expected = request.session.get("qbo_oauth_state")
    if error:
        return _qbo_fail(request, session, "QuickBooks sign-in was cancelled.")
    if not (code and realmId and state and expected
            and secrets.compare_digest(state, expected)):
        return _qbo_fail(request, session,
                         "Sign-in could not be verified — start the connection again.")
    # Genuine callback: now the nonce is spent (single use).
    request.session.pop("qbo_oauth_state", None)
    verifier = request.session.pop("qbo_code_verifier", "")
    try:
        name = qbo_api.complete_connection(session, code, realmId,
                                           _qbo_redirect_uri(request, session), verifier)
        models.set_setting(session, "qbo_company_name", name)
        models.set_setting(session, "qbo_last_error", "")
        models.audit(session, request.state.user.id, "settings.qbo", detail=f"connected {name}")
        session.commit()
    except qbo_api.QboError as e:
        return _qbo_fail(request, session, str(e))
    return RedirectResponse("/settings?qbo_ok=1#system", status_code=303)


@router.post("/settings/qbo/items-load")
def qbo_items_load(request: Request, session: Session = Depends(get_session)):
    """Fetch the QBO item list on explicit request (POST, so a page render or a
    crafted link can never trigger an outbound call) and stash it for the next
    settings render."""
    from app import qbo_api
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    try:
        request.session["qbo_items"] = qbo_api.list_items(session)
        # Creating an item needs a revenue destination, so the account list is
        # fetched in the same click rather than behind a second button.
        request.session["qbo_accounts"] = qbo_api.list_income_accounts(session)
    except qbo_api.QboError as e:
        return _qbo_fail(request, session, str(e))
    return RedirectResponse("/settings#system", status_code=303)


@router.post("/settings/qbo/disconnect")
def qbo_disconnect(request: Request, session: Session = Depends(get_session)):
    from app import qbo_api
    qbo_api.disconnect(session)
    models.set_setting(session, "qbo_company_name", "")
    models.audit(session, request.state.user.id, "settings.qbo", detail="disconnected")
    session.commit()
    return RedirectResponse("/settings#system", status_code=303)


@router.post("/settings/qbo/items")
async def save_qbo_items(request: Request, session: Session = Depends(get_session)):
    from app import qbo_api
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    models.set_setting(session, "qbo_item_scope", qbo_api.connection_scope(session))
    form = await request.form()
    models.set_setting(session, "qbo_item_service_id", (form.get("item_service") or "").strip())
    models.set_setting(session, "qbo_item_chem_id", (form.get("item_chem") or "").strip())
    session.commit()
    return RedirectResponse("/settings#system", status_code=303)


@router.post("/settings/qbo/items-provision")
async def qbo_items_provision(request: Request, session: Session = Depends(get_session)):
    """Create the two billing items in QuickBooks when they don't exist yet, then
    map them.

    Adopt-before-create: QuickBooks enforces unique item names, so a company that
    already has these items (or made them by hand) links to the originals instead
    of erroring. An existing name of a type an invoice line can't safely use is
    refused rather than mapped -- that would surface later as a billing mismatch
    on a real customer invoice instead of here, during setup.
    """
    from app import qbo_api
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    form = await request.form()
    account = (form.get("income_account") or "").strip()
    if not account:
        return _qbo_fail(request, session, "Choose the income account the new items should post to.")
    wanted = {
        "service": ((form.get("name_service") or "").strip()
                    or qbo_api.DEFAULT_ITEM_NAMES["service"], "qbo_item_service_id"),
        "chemical": ((form.get("name_chem") or "").strip()
                     or qbo_api.DEFAULT_ITEM_NAMES["chemical"], "qbo_item_chem_id"),
    }
    if wanted["service"][0] == wanted["chemical"][0]:
        return _qbo_fail(request, session,
                         "Give the service and chemical items different names.")
    scope = qbo_api.connection_scope(session)
    created = adopted = 0
    try:
        with qbo_api.connection_guard(session, scope):
            resolved = {}
            qbo_items = []
            for kind, (name, setting_key) in wanted.items():
                existing = qbo_api.find_item_by_name(session, name)
                if existing and existing["type"] not in qbo_api.SAFE_ITEM_TYPES:
                    raise qbo_api.QboError(
                        f'"{name}" already exists in QuickBooks as a '
                        f'{existing["type"]} item, which an invoice line cannot safely use. '
                        "Choose a different name, or map an existing service item by hand.")
                if existing and not existing["active"]:
                    raise qbo_api.QboError(
                        f'"{name}" exists in QuickBooks but is inactive. '
                        "Reactivate it there, or choose a different name.")
                if existing:
                    item, adopted = existing, adopted + 1
                else:
                    item, created = qbo_api.create_service_item(session, name, account), created + 1
                resolved[setting_key] = item["id"]
                qbo_items.append((kind, item))
            # Both items are resolved remotely BEFORE any mapping is written.
            # create_service_item commits to freeze its request body, so a
            # mapping written between the two creates would be committed by the
            # second one's freeze and survive this block's rollback -- leaving
            # one item mapped and the other stale, which passes the scope check
            # and only fails later, approving a real customer invoice.
            for setting_key, item_id in resolved.items():
                models.set_setting(session, setting_key, item_id)
            models.set_setting(session, "qbo_item_scope", scope)
            # Remember the account so billing can create the items it needs
            # later without sending the operator back here every month.
            models.set_setting(session, "qbo_income_account_id", account)
            for kind, item in qbo_items:
                key = "chemical" if kind == "chemical" else "service"
                if not session.scalar(select(models.QBOItemMap).where(
                        models.QBOItemMap.scope == scope, models.QBOItemMap.item_key == key)):
                    session.add(models.QBOItemMap(
                        scope=scope, item_key=key, qbo_item_id=item["id"], name=item["name"]))
            models.audit(session, request.state.user.id, "settings.qbo",
                         detail=f"item provisioning: {created} created, {adopted} adopted")
            models.set_setting(session, "qbo_last_error", "")
            session.commit()
    except qbo_api.QboError as e:
        session.rollback()
        return _qbo_fail(request, session, str(e))
    return RedirectResponse(
        f"/settings?qbo_items_created={created}&qbo_items_adopted={adopted}#system",
        status_code=303)


# A form returns to the page it was submitted from. Landing somewhere else
# loses the operator's place and their sense of what just happened -- only
# navigate away when the destination IS the outcome (a created job, a login).
CUSTOMER_REVIEW = "/settings/qbo/customers"


def _sync_back(request, session, message: str) -> RedirectResponse:
    models.set_setting(session, "qbo_last_error", message[:300])
    session.commit()
    return RedirectResponse(f"{CUSTOMER_REVIEW}?sync_error=1", status_code=303)


@router.get("/settings/qbo/customers")
def qbo_customer_review(request: Request, session: Session = Depends(get_session)):
    """Preview both directions before anything is written.

    Every number on this page comes from reads. A push writes to QuickBooks only
    after this review; an import writes nothing to QuickBooks at all.
    """
    from app import qbo_api, qbo_sync
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    try:
        push = qbo_sync.plan_push(session)
        pull = qbo_sync.plan_import(session)
    except (qbo_api.QboError, qbo_sync.SyncError) as exc:
        return _qbo_fail(request, session, str(exc))
    return templates.TemplateResponse(request, "settings/qbo_customers.html", {
        "push": push, "pull": pull, "company": models.get_setting(session, "qbo_company_name", ""),
        "environment": models.get_setting(session, "qbo_environment", "sandbox"),
        # Counts only; the error TEXT comes from the stored setting so a crafted
        # link cannot render attacker copy inside the admin UI.
        "result": {
            "pushed": _parse_count(request.query_params.get("pushed")),
            "imported": _parse_count(request.query_params.get("imported")),
            "linked": _parse_count(request.query_params.get("linked")),
            "updated": _parse_count(request.query_params.get("updated")),
            "held": _parse_count(request.query_params.get("held")),
            "error": bool(request.query_params.get("sync_error")),
        },
        "last_error": models.get_setting(session, "qbo_last_error", ""),
    })


@router.post("/settings/qbo/customers/push")
async def qbo_customer_push(request: Request, session: Session = Depends(get_session)):
    """Create missing customers, link exact matches, and apply approved updates."""
    from app import qbo_api, qbo_sync
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    form = await request.form()
    approved = {str(v) for v in form.getlist("approve")}
    try:
        result = qbo_sync.apply_push(session, str(form.get("digest", "")), approved,
                                     request.state.user.id)
    except (qbo_api.QboError, qbo_sync.SyncError) as exc:
        session.rollback()
        return _sync_back(request, session, str(exc))
    if result["errors"]:
        return _sync_back(request, session, "; ".join(result["errors"])[:300])
    models.set_setting(session, "qbo_last_error", "")
    session.commit()
    return RedirectResponse(
        f"{CUSTOMER_REVIEW}?pushed={result['created']}&linked={result['linked']}"
        f"&updated={result['updated']}&held={result['skipped']}",
        status_code=303)


@router.post("/settings/qbo/customers/import")
async def qbo_customer_import(request: Request, session: Session = Depends(get_session)):
    """Pull the QuickBooks roster into OpenPoolPro. Reads QuickBooks only."""
    from app import qbo_api, qbo_sync
    if not qbo_api.connected(session):
        return _qbo_fail(request, session, "Connect QuickBooks first.")
    form = await request.form()
    decisions = {}
    for key in form.keys():
        if key.startswith("decide:"):
            decisions[key.removeprefix("decide:")] = str(form.get(key))
    try:
        result = qbo_sync.apply_import(session, str(form.get("digest", "")), decisions,
                                       request.state.user.id)
    except (qbo_api.QboError, qbo_sync.SyncError) as exc:
        session.rollback()
        return _sync_back(request, session, str(exc))
    models.set_setting(session, "qbo_last_error", "")
    session.commit()
    return RedirectResponse(
        f"{CUSTOMER_REVIEW}?imported={result['created']}&linked={result['linked']}"
        f"&held={result['skipped']}", status_code=303)


@router.post("/settings/sign-messages")
async def save_sign_messages(request: Request, session: Session = Depends(get_session)):
    from app import sign_messages
    form = await request.form()
    for key in sign_messages.DEFAULTS:
        val = form.get(key)
        if val is not None:
            models.set_setting(session, key, val)
    session.commit()
    return RedirectResponse("/settings#contract", status_code=303)


@router.post("/settings/require-2fa")
async def save_require_2fa(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    on = "1" if form.get("require_2fa") == "on" else "0"
    models.set_setting(session, "require_2fa", on)
    models.audit(session, request.state.user.id, "settings.require_2fa",
                 detail="enabled" if on == "1" else "disabled")
    session.commit()
    request.app.state.require_2fa = on == "1"  # keep the middleware's cache in sync
    return RedirectResponse("/settings#system", status_code=303)


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


@router.get("/settings/logs")
def logs_page(request: Request, level: str = "", q: str = ""):
    """Admin › Logs: the recent in-memory log lines, newest first, filtered by
    level and text. Download returns the same lines as plain text."""
    from app.observability import RECENT
    order = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
    floor = order.get(level, 0)
    needle = (q or "").strip().lower()[:80]
    lines = [r for r in reversed(RECENT.lines)
             if order.get(r["level"], 1) >= floor
             and (not needle or needle in r["line"].lower())][:500]
    sources = sorted({r["source"] for r in RECENT.lines})
    return templates.TemplateResponse(request, "settings/logs.html", {
        "lines": lines, "level": level, "q": needle, "sources": sources,
        "total": len(RECENT.lines)})


@router.get("/settings/logs.txt")
def logs_download():
    from fastapi.responses import PlainTextResponse

    from app.observability import RECENT
    return PlainTextResponse("\n".join(r["line"] for r in RECENT.lines) + "\n",
                             headers={"Content-Disposition": "attachment; filename=openpoolpro-recent.log"})


@router.get("/settings/about")
def about_page(request: Request):
    """Admin › About: what is running, and where releases come from."""
    import platform
    import sys

    return templates.TemplateResponse(request, "settings/about.html", {
        "version": config.APP_VERSION, "python": sys.version.split()[0],
        "platform": platform.platform(terse=True)})

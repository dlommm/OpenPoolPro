import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session
from app.main import templates

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    tiers = session.scalars(select(models.RateTier).order_by(
        models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()
    chems = session.scalars(select(models.Chemical).order_by(models.Chemical.name)).all()
    tasks = session.scalars(select(models.TaskTemplate)
                            .order_by(models.TaskTemplate.sort_order)).all()
    backups_dir = os.path.join(config.DATA_DIR, "backups")
    backups = sorted(os.listdir(backups_dir), reverse=True) if os.path.isdir(backups_dir) else []
    return templates.TemplateResponse(request, "settings/index.html", {
        "tiers": tiers, "chems": chems, "tasks": tasks, "backups": backups,
        "markup": models.get_setting(session, "chem_markup_percent", "0"),
        "business_name": models.get_setting(session, "business_name", ""),
        "backup_error": models.get_setting(session, "last_backup_error", "")})


@router.post("/settings/tiers")
async def save_tiers(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    rows = zip(form.getlist("tier_id"), form.getlist("max_gallons"),
               form.getlist("rate_dollars"), form.getlist("label"))
    for tid, max_g, rate, label in rows:
        if not rate.strip():
            if tid:
                session.delete(session.get(models.RateTier, int(tid)))
            continue
        try:
            t = session.get(models.RateTier, int(tid)) if tid else models.RateTier(rate_cents=0)
            t.max_gallons = int(max_g) if max_g.strip() else None
            t.rate_cents = round(float(rate) * 100)
            t.label = label
            session.add(t)
        except ValueError:
            continue
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/chemicals")
async def save_chemicals(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    rows = zip(form.getlist("chem_id"), form.getlist("name"),
               form.getlist("unit"), form.getlist("cost_dollars"))
    for cid, name, unit, cost in rows:
        if cid:
            c = session.get(models.Chemical, int(cid))
            if not name.strip():
                c.active = False
                continue
            try:
                c.name, c.unit = name, unit
                c.cost_per_unit_cents = round(float(cost or 0) * 100)
                c.active = form.get(f"active_{cid}") == "on"
            except ValueError:
                continue
        elif name.strip():
            try:
                session.add(models.Chemical(name=name, unit=unit,
                                            cost_per_unit_cents=round(float(cost or 0) * 100)))
            except ValueError:
                continue
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/tasks")
async def save_tasks(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for i, (tid, label) in enumerate(zip(form.getlist("task_id"), form.getlist("label"))):
        if tid:
            t = session.get(models.TaskTemplate, int(tid))
            if not label.strip():
                t.active = False
            else:
                t.label, t.sort_order = label, i
        elif label.strip():
            session.add(models.TaskTemplate(label=label, sort_order=i))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/general")
async def save_general(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    models.set_setting(session, "chem_markup_percent", form.get("chem_markup_percent", "0") or "0")
    models.set_setting(session, "business_name", form.get("business_name", ""))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/backups/{filename}")
def download_backup(filename: str):
    if "/" in filename or ".." in filename:
        return HTMLResponse("nope", status_code=400)
    path = os.path.join(config.DATA_DIR, "backups", filename)
    if not os.path.exists(path):
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path, filename=filename)

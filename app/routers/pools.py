from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session, get_or_404
from app.deps import require_perm
from app.main import templates
from app.rates import gallons

router = APIRouter()


def _tier_rate_for(session, g: int, kind: str = "pool") -> int:
    from app.rates import tier_rate_cents
    return tier_rate_cents(session, kind, g)


@router.get("/pools/gallons-preview", response_class=HTMLResponse)
def gallons_preview(shape: str = "rectangle", length_ft: str = "", width_ft: str = "",
                    avg_depth_ft: str = "", session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    try:
        g = gallons(shape, float(length_ft or 0), float(width_ft or 0) or None,
                    float(avg_depth_ft or 0))
    except (ValueError, TypeError):
        return HTMLResponse("<span class='text-mist/60'>—</span>")
    html = f"<span class='font-semibold'>≈ {g:,} gal</span>"
    if user.can("view_money"):
        rate = _tier_rate_for(session, g)
        html += f"<span class='text-mist'> — suggested rate ${rate / 100:.2f}</span>"
    return HTMLResponse(html)


def _pool_form(request, session, pool, property_id, ptag: str = ""):
    photos = []
    ptag = ptag if ptag in models.PHOTO_TAGS else ""  # tolerant: unknown -> all
    if pool is not None:
        stmt = select(models.Photo).where(
            models.Photo.entity_type == "pool",
            models.Photo.entity_id == pool.id)
        if ptag:
            stmt = stmt.where(models.Photo.tag == ptag)
        photos = session.scalars(stmt.order_by(models.Photo.taken_at.desc())).all()
    return templates.TemplateResponse(request, "pools/form.html",
        {"pool": pool, "property_id": property_id, "photos": photos, "ptag": ptag})


@router.get("/properties/{pid}/pools/new")
def new_pool(request: Request, pid: int, session: Session = Depends(get_session),
            user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Property, pid)
    return _pool_form(request, session, None, pid)


@router.get("/pools/{pool_id}/edit")
def edit_pool(request: Request, pool_id: int, ptag: str = "",
             session: Session = Depends(get_session),
             user=Depends(require_perm("manage_customers"))):
    pool = get_or_404(session, models.Pool, pool_id)
    return _pool_form(request, session, pool, pool.property_id, ptag)


def _apply(pool: models.Pool, form) -> None:
    pool.label = form.get("label", "Main pool")
    pool.shape = form.get("shape", "rectangle")
    pool.length_ft = float(form.get("length_ft") or 0)
    width = form.get("width_ft") or ""
    pool.width_ft = float(width) if width else None
    pool.avg_depth_ft = float(form.get("avg_depth_ft") or 0)
    pool.gallons = gallons(pool.shape, pool.length_ft, pool.width_ft, pool.avg_depth_ft)
    pool.water_type = form.get("water_type", "chlorine")
    pool.surface = form.get("surface", "plaster")
    pool.notes = form.get("notes", "")


async def _save(request: Request, session: Session, pool: models.Pool, redirect_cid: int):
    form = await request.form()
    try:
        _apply(pool, form)
    except (ValueError, TypeError):
        return HTMLResponse("Invalid dimensions: must be > 0 and ≤ 100 ft", status_code=400)
    session.add(pool)
    session.commit()
    return RedirectResponse(f"/customers/{redirect_cid}", status_code=303)


@router.post("/properties/{pid}/pools/new")
async def create_pool(request: Request, pid: int, session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    prop = get_or_404(session, models.Property, pid)
    return await _save(request, session, models.Pool(property_id=pid), prop.customer_id)


@router.post("/pools/{pool_id}/edit")
async def update_pool(request: Request, pool_id: int, session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    pool = get_or_404(session, models.Pool, pool_id)
    return await _save(request, session, pool, pool.property.customer_id)


@router.post("/pools/{pool_id}/deactivate")
def deactivate_pool(pool_id: int, session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    pool = get_or_404(session, models.Pool, pool_id)
    pool.active = False
    session.commit()
    return RedirectResponse(f"/customers/{pool.property.customer_id}", status_code=303)


def _parse_date_tolerant(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@router.post("/pools/{pool_id}/equipment/new")
def add_equipment(pool_id: int, kind: str = Form("pump"), brand: str = Form(""),
                  model: str = Form(""), serial: str = Form(""),
                  install_date: str = Form(""), warranty_end: str = Form(""),
                  notes: str = Form(""),
                  session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    pool = get_or_404(session, models.Pool, pool_id)
    session.add(models.Equipment(
        pool_id=pool_id, kind=kind, brand=brand, model=model, serial=serial,
        install_date=_parse_date_tolerant(install_date),
        warranty_end=_parse_date_tolerant(warranty_end), notes=notes))
    session.commit()
    return RedirectResponse(f"/pools/{pool_id}/edit", status_code=303)


@router.post("/equipment/{eid}/delete")
def delete_equipment(eid: int, session: Session = Depends(get_session),
                     user=Depends(require_perm("manage_customers"))):
    eq = get_or_404(session, models.Equipment, eid)
    pool_id = eq.pool_id
    session.delete(eq)
    session.commit()
    return RedirectResponse(f"/pools/{pool_id}/edit", status_code=303)

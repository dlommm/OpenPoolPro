import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inventory, models
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _f(val) -> float | None:
    """Same tolerant-parse shape as visits.py's _f: blank/garbage/non-finite -> None."""
    val = (val or "").strip()
    if not val:
        return None
    try:
        f = float(val)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _reorder_field(val) -> tuple[bool, float | None]:
    """Tolerant parse for reorder point/qty inputs. Unlike `_f`, blank is a
    deliberate, valid value here (alerting off for that chemical), so it
    must be distinguishable from garbage/non-finite/negative input, which is
    an error. Returns (ok, value)."""
    val = (val or "").strip()
    if not val:
        return True, None
    try:
        f = float(val)
    except ValueError:
        return False, None
    if not math.isfinite(f) or f < 0:
        return False, None
    return True, f


def _context(session: Session, error: str | None = None,
             restock_id: int | None = None) -> dict:
    chems = session.scalars(select(models.Chemical).order_by(models.Chemical.name)).all()
    active_chems = [c for c in chems if c.active]
    low_ids = {c.id for c in inventory.low_stock(session)}
    history = session.scalars(
        select(models.Purchase).order_by(models.Purchase.at.desc()).limit(50)).all()
    purchases = [p for p in history if not p.note.startswith("adjust: ")]
    adjustments = [p for p in history if p.note.startswith("adjust: ")]
    users_by_id = {u.id: u for u in session.scalars(select(models.User)).all()}
    restock_chem = session.get(models.Chemical, restock_id) if restock_id else None
    return {"chems": chems, "active_chems": active_chems, "low_ids": low_ids,
            "purchases": purchases, "adjustments": adjustments,
            "users_by_id": users_by_id, "error": error,
            "total_value_cents": inventory.inventory_value_cents(session),
            "reorder_items": inventory.reorder_list(session),
            "item_count": len(active_chems),
            "low_count": len(low_ids),
            "out_count": sum(1 for c in active_chems if c.stock_qty <= 0),
            "restock_id": restock_chem.id if restock_chem else None,
            "restock_qty": restock_chem.reorder_qty if restock_chem else None}


@router.get("/inventory")
def inventory_page(request: Request, restock: int | None = None,
                   session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "inventory.html",
                                      _context(session, restock_id=restock))


@router.post("/inventory/purchase")
async def purchase(request: Request, session: Session = Depends(get_session)):
    form = await request.form()

    def _error(message: str):
        return templates.TemplateResponse(request, "inventory.html",
            _context(session, error=message), status_code=200)

    try:
        chem = session.get(models.Chemical, int(form.get("chemical_id", "")))
    except (TypeError, ValueError):
        chem = None
    if chem is None:
        return _error("Select a chemical")

    quantity = _f(form.get("quantity"))
    if quantity is None:
        return _error("Enter a valid quantity")

    raw_cost = (form.get("cost_dollars") or "").strip()
    if not raw_cost:
        cost_dollars = 0.0  # blank cost is allowed (donation/found stock)
    else:
        cost_dollars = _f(raw_cost)
        if cost_dollars is None:
            return _error("Enter a valid cost")

    cost_cents = round(cost_dollars * 100)
    update_cost = form.get("update_cost") == "on"
    note = form.get("note", "")
    result = inventory.record_purchase(session, chem, quantity, cost_cents, note,
                                       request.state.user.id, update_cost=update_cost)
    if result is None:
        return _error("Invalid purchase — check quantity and cost")
    session.commit()
    return RedirectResponse("/inventory", status_code=303)


@router.post("/inventory/adjust/{chem_id}")
async def adjust(chem_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    chem = get_or_404(session, models.Chemical, chem_id)

    def _error(message: str):
        return templates.TemplateResponse(request, "inventory.html",
            _context(session, error=message), status_code=200)

    target = _f(form.get("target"))
    if target is None:
        return _error("Enter a valid on-hand amount")
    note = form.get("note", "")
    result = inventory.set_on_hand(session, chem, target, note, request.state.user.id)
    if result is None:
        return _error("Invalid on-hand amount")
    session.commit()
    return RedirectResponse("/inventory", status_code=303)


@router.post("/inventory/reorder/{chem_id}")
async def reorder(chem_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    chem = get_or_404(session, models.Chemical, chem_id)

    def _error(message: str):
        return templates.TemplateResponse(request, "inventory.html",
            _context(session, error=message), status_code=200)

    ok_point, reorder_point = _reorder_field(form.get("reorder_point"))
    ok_qty, reorder_qty = _reorder_field(form.get("reorder_qty"))
    if not ok_point or not ok_qty:
        return _error("Enter a valid reorder point and quantity (or leave blank to turn off alerting)")

    chem.reorder_point = reorder_point
    chem.reorder_qty = reorder_qty
    session.commit()
    return RedirectResponse("/inventory", status_code=303)

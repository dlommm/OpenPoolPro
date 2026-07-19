"""Pure stock-tracking service: visit-delta decrement, purchase logging,
manual on-hand adjustment, and low-stock lookup.

No FastAPI here — this module is a plain mutation/query layer. Callers
(routers) own the session's transaction boundary and commit; functions here
only mutate in-memory ORM state / add pending rows.
"""
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def apply_visit_delta(session: Session, visit_id: int,
                      prior_lines: dict[int, float], new_lines: dict[int, float]) -> None:
    """Adjust stock_qty by the delta (new - prior) for every chemical touched
    by a visit's chemical lines, over the union of both maps' keys. A save
    with unchanged lines nets to zero; removing a line restores the stock
    that line had consumed. `visit_id` identifies the visit for callers/audit
    context only — the two summed maps already fully determine the deltas.
    """
    for chem_id in set(prior_lines) | set(new_lines):
        delta = new_lines.get(chem_id, 0.0) - prior_lines.get(chem_id, 0.0)
        if delta == 0:
            continue
        chem = session.get(models.Chemical, chem_id)
        if chem is None:
            continue
        chem.stock_qty -= delta


def _valid_quantity(quantity: float) -> bool:
    try:
        return math.isfinite(quantity) and quantity > 0
    except TypeError:
        return False


def _valid_cost(total_cost_cents: int) -> bool:
    try:
        return math.isfinite(total_cost_cents) and total_cost_cents >= 0
    except TypeError:
        return False


def record_purchase(session: Session, chemical: models.Chemical, quantity: float,
                    total_cost_cents: int, note: str, user_id: int | None,
                    update_cost: bool = True) -> models.Purchase | None:
    """Log a purchase: stock_qty += quantity; when `update_cost` and the
    purchase is valid, reprice the catalog to round(total_cost_cents /
    quantity) — this only changes what FUTURE visits freeze as their cost.
    Past visits' frozen VisitChemical rows are never touched.

    Invalid input (non-finite/non-positive quantity, negative or non-finite
    cost) returns None and mutates nothing.
    """
    if not _valid_quantity(quantity) or not _valid_cost(total_cost_cents):
        return None
    chemical.stock_qty += quantity
    purchase = models.Purchase(chemical_id=chemical.id, quantity=quantity,
                               total_cost_cents=total_cost_cents, note=note or "",
                               user_id=user_id)
    session.add(purchase)
    if update_cost:
        chemical.cost_per_unit_cents = round(total_cost_cents / quantity)
    return purchase


def set_on_hand(session: Session, chemical: models.Chemical, target: float,
                note: str, user_id: int | None) -> models.Purchase | None:
    """Set stock_qty to an exact recounted value, logging the signed delta
    (target - current) as a zero-cost adjustment purchase row whose note is
    prefixed "adjust: ". Non-finite target returns None and mutates nothing.
    """
    try:
        if not math.isfinite(target):
            return None
    except TypeError:
        return None
    delta = target - chemical.stock_qty
    purchase = models.Purchase(chemical_id=chemical.id, quantity=delta, total_cost_cents=0,
                               note=f"adjust: {note or ''}", user_id=user_id)
    session.add(purchase)
    chemical.stock_qty = target
    return purchase


def low_stock(session: Session) -> list[models.Chemical]:
    """Active chemicals at or below their reorder point, ordered by name.
    A null reorder_point means alerting is disabled for that chemical.
    """
    return session.scalars(
        select(models.Chemical)
        .where(models.Chemical.active.is_(True),
               models.Chemical.reorder_point.is_not(None),
               models.Chemical.stock_qty <= models.Chemical.reorder_point)
        .order_by(models.Chemical.name)
    ).all()

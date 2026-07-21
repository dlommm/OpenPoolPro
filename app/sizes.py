"""Helpers for the managed pool/spa size library (models.SizePreset)."""
from sqlalchemy import select

from app import models
from app.rates import gallons


def _row_gallons(sp: "models.SizePreset") -> int:
    if sp.ref_gallons:  # explicit gallons (e.g. spas, where the pool formula doesn't fit)
        return sp.ref_gallons
    try:
        return gallons(sp.shape, sp.length_ft, sp.width_ft, sp.avg_depth_ft)
    except (ValueError, TypeError):
        return 0


def _clean(v):
    """Whole floats render as ints (32.0 -> 32) so prefilled dims look tidy."""
    if v is None:
        return None
    return int(v) if float(v).is_integer() else v


def _to_dict(sp: "models.SizePreset") -> dict:
    return {"id": sp.id, "kind": sp.kind, "label": sp.label, "shape": sp.shape,
            "length_ft": _clean(sp.length_ft), "width_ft": _clean(sp.width_ft),
            "avg_depth_ft": _clean(sp.avg_depth_ft), "ref_gallons": _row_gallons(sp)}


def presets_for_form(session) -> dict:
    """Active size presets grouped by kind: {'pool': [dict...], 'spa': [dict...]}."""
    rows = session.scalars(
        select(models.SizePreset).where(models.SizePreset.active)
        .order_by(models.SizePreset.sort_order, models.SizePreset.id)).all()
    out = {"pool": [], "spa": []}
    for sp in rows:
        out.setdefault(sp.kind, [])
        out[sp.kind].append(_to_dict(sp))
    return out


def all_as_dicts(session) -> list[dict]:
    """Every size preset (both kinds, incl. inactive) as dicts with computed gallons."""
    rows = session.scalars(
        select(models.SizePreset)
        .order_by(models.SizePreset.kind, models.SizePreset.sort_order, models.SizePreset.id)).all()
    return [_to_dict(sp) for sp in rows]


def prefill_from_id(session, preset_id) -> dict | None:
    """Prefill dict for the pool form from a SizePreset id, or None."""
    try:
        pid = int(preset_id)
    except (TypeError, ValueError):
        return None
    sp = session.get(models.SizePreset, pid)
    return _to_dict(sp) if sp is not None else None

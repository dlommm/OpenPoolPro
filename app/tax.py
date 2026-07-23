"""Sales-tax engine: single source of truth for rate resolution + amounts.

Rates are stored as integer PPM (parts per million of the taxable base) =
percent x 10_000, so 6.625% -> 66250. All money is integer cents. Tax is
disabled by default; with tax off or a zero rate every surface renders
exactly as it did before this module existed.
"""
from __future__ import annotations


def parse_percent(s: str) -> int:
    """Percent string -> PPM int. Blank/garbage/negative -> 0."""
    try:
        pct = float((s or "").strip())
    except (TypeError, ValueError):
        return 0
    if pct <= 0:
        return 0
    return round(pct * 10_000)


def format_ppm(ppm: int) -> str:
    """PPM int -> percent string with trailing zeros trimmed. 66250 -> '6.625'."""
    pct = (ppm or 0) / 10_000
    s = f"{pct:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def tax_cents(taxable_cents: int, rate_ppm: int) -> int:
    """Tax amount in cents for a taxable base at a PPM rate."""
    return round((taxable_cents or 0) * (rate_ppm or 0) / 1_000_000)


def tax_enabled(session) -> bool:
    from app import models
    return models.get_setting(session, "sales_tax_enabled", "0") == "1"


def resolve_rate_ppm(session, state: str | None) -> int:
    """State -> PPM. 0 if tax disabled. TaxRate row for the state, else the
    configured default. Never raises."""
    from app import models
    if not tax_enabled(session):
        return 0
    key = (state or "").strip().upper()
    if key:
        row = session.get(models.TaxRate, key)
        if row is not None:
            return row.rate_ppm or 0
    try:
        return int(models.get_setting(session, "sales_tax_default_ppm", "0") or 0)
    except ValueError:
        return 0


def doc_tax_rate_ppm(session, doc) -> int:
    """Frozen doc.tax_rate_ppm if set, else live-resolve from doc.property.state."""
    frozen = getattr(doc, "tax_rate_ppm", None)
    if frozen is not None:
        return frozen
    state = getattr(getattr(doc, "property", None), "state", None)
    return resolve_rate_ppm(session, state)

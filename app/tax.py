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

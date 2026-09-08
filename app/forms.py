"""Tolerant form-field parsers shared by the routers.

These existed as four private copies of parse_date, four of parse_int, two of
cents, and two of parse_page scattered across routers — and the copies had
already drifted: one cents() stripped thousands separators and one didn't,
one allowed zero and one didn't, and only the quotes copy guarded against
non-finite input ("inf" made the others raise OverflowError in round(), i.e.
a 500 from a form field). One implementation each, drift-proof.

House rule (see the original customers.py docstrings): a typo in a form field
is a rejected/ignored value, never a 500 — so everything here maps blank,
malformed, or out-of-range input to None (or a safe default) instead of
raising.
"""
import math
from datetime import date


def parse_date_tolerant(s: str | None) -> date | None:
    """Blank or malformed -> None. A bare date.fromisoformat turns a typo
    into a 500."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_int_tolerant(s: str | None) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_float_tolerant(s: str | None) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def parse_page(raw: str | None) -> int:
    """Tolerant page parse: garbage or non-positive values fall back to 1."""
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return 1
    return p if p >= 1 else 1


def cents(dollars: str | None, *, minimum: int = 0) -> int | None:
    """Dollar string -> integer cents; blank, malformed, non-finite, or below
    `minimum` -> None. Callers choose the floor: a rate override may be 0
    (minimum=0, the default), a recorded payment must be positive
    (minimum=1) — a negative value in either would flow straight into
    billing. Accepts "$" and thousands separators ("$1,200.50")."""
    text = (dollars or "").strip().lstrip("$").replace(",", "")
    if not text:
        return None
    try:
        f = float(text)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    scaled = f * 100
    # SQLite INTEGER is signed 64-bit. Even a finite dollar float can
    # overflow during scaling or when the resulting cents reach SQLite.
    if not math.isfinite(scaled) or scaled >= 2**63:
        return None
    value = round(scaled)
    return value if value >= minimum else None

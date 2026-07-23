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


def _flag(session, key: str) -> bool:
    from app import models
    return models.get_setting(session, key, "1") == "1"


def effective_labor_cents(doc) -> int:
    """Override if set, else the frozen labor (visits: rate_cents; jobs: labor_cents)."""
    override = getattr(doc, "labor_override_cents", None)
    if override is not None:
        return override
    if getattr(doc, "labor_cents", None) is not None:
        return doc.labor_cents
    return getattr(doc, "rate_cents", 0) or 0


def chem_billable_cents(session, visit) -> int:
    from app import models, qbo
    try:
        markup = float(models.get_setting(session, "chem_markup_percent", "0"))
    except ValueError:
        markup = 0.0
    return sum(qbo.billable_for_vc(vc, markup) for vc in visit.chemicals)


def visit_breakdown(session, visit) -> dict:
    labor = effective_labor_cents(visit)
    chems = chem_billable_cents(session, visit)
    rate_ppm = doc_tax_rate_ppm(session, visit)
    taxable = (labor if _flag(session, "tax_weekly") else 0) + \
              (chems if _flag(session, "tax_chems") else 0)
    t = tax_cents(taxable, rate_ppm)
    return {"labor": labor, "chems": chems, "taxable": taxable,
            "rate_ppm": rate_ppm, "tax": t, "total": labor + chems + t}


def job_breakdown(session, job) -> dict:
    labor = effective_labor_cents(job)
    rate_ppm = doc_tax_rate_ppm(session, job)
    taxable = labor if _flag(session, "tax_labor") else 0
    t = tax_cents(taxable, rate_ppm)
    return {"labor": labor, "taxable": taxable, "rate_ppm": rate_ppm,
            "tax": t, "total": labor + t}

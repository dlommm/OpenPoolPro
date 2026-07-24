"""Contract-template tokens: the single text<->data binding, shared by the PDF
renderer (app/contract_pdf.py) and the live preview (app/routers/contract_template.py)
so they never disagree. resolve_tokens turns the live objects into display
strings; apply_tokens substitutes {{token}} in body text."""
import re

from app import tax as tax_mod

_TOKEN_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")

# (token, human label, group) — drives resolution AND the editor palette order.
TOKENS = [
    ("company_name", "Company name", "Company"),
    ("company_address", "Company address", "Company"),
    ("company_phone", "Company phone", "Company"),
    ("company_email", "Company email", "Company"),
    ("company_license", "License #", "Company"),
    ("customer_name", "Customer name", "Customer & property"),
    ("property_label", "Property label", "Customer & property"),
    ("property_address", "Property address", "Customer & property"),
    ("gate_code", "Gate code", "Customer & property"),
    ("city", "City", "Customer & property"),
    ("state", "State", "Customer & property"),
    ("zip", "ZIP", "Customer & property"),
    ("pool_label", "Pool label", "Pool"),
    ("pool_gallons", "Pool gallons", "Pool"),
    ("pool_water_type", "Water type", "Pool"),
    ("pool_surface", "Surface", "Pool"),
    ("pool_dimensions", "Dimensions", "Pool"),
    ("weekly_price", "Weekly price", "Pricing & tax"),
    ("spa_price", "Spa price", "Pricing & tax"),
    ("open_close_price", "Open/close price", "Pricing & tax"),
    ("green_clean_price", "Green-clean price", "Pricing & tax"),
    ("storm_price", "Storm price", "Pricing & tax"),
    ("service_call_price", "Service-call price", "Pricing & tax"),
    ("season_weeks", "Season weeks", "Pricing & tax"),
    ("season_total", "Season total (weeks x weekly)", "Pricing & tax"),
    ("sales_tax", "Sales tax", "Pricing & tax"),
]
TOKEN_NAMES = {t[0] for t in TOKENS}


def _money(cents) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def _g(obj, attr, default=""):
    return getattr(obj, attr, default) if obj is not None else default


def resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm: int = 0) -> dict:
    company = company or {}
    weekly = _g(contract, "weekly_cents", 0) or 0
    weeks = _g(contract, "weeks", 0) or 0
    open_close = _g(contract, "open_close_cents", 0) or 0
    # season_total + sales_tax mirror app/contract_pdf.py::_rates_table exactly.
    season_total_cents = weeks * weekly
    tax_cents = tax_mod.tax_cents(season_total_cents + open_close, tax_rate_ppm) if tax_rate_ppm else 0

    gallons = _g(pool, "gallons", 0) or 0
    length = _g(pool, "length_ft", 0) or 0
    width = _g(pool, "width_ft", 0) or 0
    depth = _g(pool, "avg_depth_ft", 0) or 0
    dims = ""
    if length or width:
        dims = f"{length:g}x{width:g}" + (f" ft, avg {depth:g} ft deep" if depth else " ft")

    return {
        "company_name": company.get("business_name") or "",
        "company_address": company.get("company_address") or "",
        "company_phone": company.get("company_phone") or "",
        "company_email": company.get("company_email") or "",
        "company_license": company.get("company_license") or "",
        "customer_name": _g(customer, "name", "") or "",
        "property_label": _g(prop, "label", "") or "",
        "property_address": _g(prop, "address", "") or "",
        "gate_code": _g(prop, "gate_code", "") or "",
        "city": _g(prop, "city", "") or "",
        "state": _g(prop, "state", "") or "",
        "zip": _g(prop, "zip", "") or "",
        "pool_label": _g(pool, "label", "") or "",
        "pool_gallons": f"{gallons:,}" if gallons else "",
        "pool_water_type": _g(pool, "water_type", "") or "",
        "pool_surface": _g(pool, "surface", "") or "",
        "pool_dimensions": dims,
        "weekly_price": _money(weekly),
        "spa_price": _money(_g(contract, "spa_cents", 0) or 0),
        "open_close_price": _money(open_close),
        "green_clean_price": _money(_g(contract, "green_clean_cents", 0) or 0),
        "storm_price": _money(_g(contract, "storm_cents", 0) or 0),
        "service_call_price": _money(_g(contract, "service_call_cents", 0) or 0),
        "season_weeks": str(weeks),
        "season_total": _money(season_total_cents),
        "sales_tax": _money(tax_cents),
    }


def apply_tokens(text, ctx: dict) -> str:
    if not text:
        return ""
    return _TOKEN_RE.sub(lambda m: str(ctx.get(m.group(1), "")), text)

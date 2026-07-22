from sqlalchemy import select

from app import models

SHAPE_MULT = {"rectangle": 7.5, "oval": 5.9, "kidney": 6.0, "grecian": 7.0}
MAX_DIM_FT = 100


def largest_pool_gallons(prop, kind: str = "pool") -> int:
    """Gallons of the largest active pool of the given kind on a property (0 if none)."""
    return max((p.gallons for p in prop.pools if p.active and p.kind == kind), default=0)


def template_price_cents(template, pool_gallons: int) -> tuple[int | None, str]:
    """Resolve a job-template price for a pool size.

    Returns (price_cents, status):
      - ("flat")     the template has no size matrix -> caller uses the flat labor
      - ("matched")  a size row matched -> price_cents is set
      - ("unmatched") the template HAS a matrix but no row fits -> owner must price it
    """
    if not template.prices:
        return (None, "flat")
    rows = sorted(template.prices, key=lambda r: (r.max_gallons is None, r.max_gallons or 0))
    for r in rows:
        if r.max_gallons is None or pool_gallons <= r.max_gallons:
            return (r.price_cents, "matched")
    return (None, "unmatched")


def gallons(shape: str, length_ft: float, width_ft: float | None, avg_depth_ft: float) -> int:
    dims = [length_ft, avg_depth_ft] + ([] if shape == "round" else [width_ft or 0])
    if any(d is None or d <= 0 or d > MAX_DIM_FT for d in dims):
        raise ValueError("dimensions must be > 0 and <= 100 ft")
    if shape == "round":
        r = length_ft / 2
        return round(r * r * 3.14 * avg_depth_ft * 7.5)
    if shape not in SHAPE_MULT:
        raise ValueError(f"unknown shape: {shape}")
    return round(length_ft * width_ft * avg_depth_ft * SHAPE_MULT[shape])


def tier_rate_cents(session, kind: str, gallons_val: int) -> int:
    tiers = session.scalars(
        select(models.RateTier)
        .where(models.RateTier.kind == kind)
        .order_by(models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)
    ).all()
    for t in tiers:
        if t.max_gallons is None or gallons_val <= t.max_gallons:
            return t.rate_cents
    return 0


def service_type(prop: models.Property) -> str:
    pools = [p for p in prop.pools if p.active]
    has_pool = any(p.kind == "pool" for p in pools)
    has_spa = any(p.kind == "spa" for p in pools)
    if has_pool and has_spa:
        return "pool_spa"
    if has_spa:
        return "spa"
    return "pool"


def default_labor_rate_cents(session) -> int:
    """The owner's default labor rate ($/hr) in cents (setting `labor_rate_cents`)."""
    try:
        return int(models.get_setting(session, "labor_rate_cents", "18000") or 18000)
    except (TypeError, ValueError):
        return 18000


def template_hours(template, pool_gallons: int) -> tuple[float | None, str]:
    """Resolve a job template's estimated hours for a pool size, mirroring the
    old price matrix but in hours. Returns (hours, status):
      flat      -> no size matrix; use template.est_hours
      matched   -> a size row fit; hours is that row's hours
      unmatched -> matrix exists but no row fits; owner must enter hours
    """
    rows = [p for p in template.prices]
    if not rows:
        return (template.est_hours, "flat")
    rows = sorted(rows, key=lambda r: (r.max_gallons is None, r.max_gallons or 0))
    for r in rows:
        if r.max_gallons is None or pool_gallons <= r.max_gallons:
            return (r.hours, "matched")
    return (None, "unmatched")


def job_labor_cents(rate_cents: int, hours: float) -> int:
    return round((rate_cents or 0) * (hours or 0))


def effective_rate_cents(session, prop: models.Property) -> int:
    pools = [p for p in prop.pools if p.active]
    # Pool component: property override wins (legacy semantics), else pool-tier by
    # largest pool-kind gallons, else 0 when there is no pool.
    if prop.rate_override_cents is not None:
        pool_cents = prop.rate_override_cents
    elif any(p.kind == "pool" for p in pools):
        largest = max((p.gallons for p in pools if p.kind == "pool"), default=0)
        pool_cents = tier_rate_cents(session, "pool", largest)
    else:
        pool_cents = 0
    # Spa component: spa override wins, else spa-tier by largest spa-kind gallons,
    # else 0 when there is no spa.
    if prop.spa_rate_override_cents is not None:
        spa_cents = prop.spa_rate_override_cents
    elif any(p.kind == "spa" for p in pools):
        largest_spa = max((p.gallons for p in pools if p.kind == "spa"), default=0)
        spa_cents = tier_rate_cents(session, "spa", largest_spa)
    else:
        spa_cents = 0
    return pool_cents + spa_cents

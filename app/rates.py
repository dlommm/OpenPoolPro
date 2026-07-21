from sqlalchemy import select

from app import models

SHAPE_MULT = {"rectangle": 7.5, "oval": 5.9, "kidney": 6.0, "grecian": 7.0}
MAX_DIM_FT = 100


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

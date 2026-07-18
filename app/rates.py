from sqlalchemy import select

from app import models

SHAPE_MULT = {"rectangle": 7.5, "oval": 5.9, "kidney": 6.0}
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


def effective_rate_cents(session, prop: models.Property) -> int:
    if prop.rate_override_cents is not None:
        return prop.rate_override_cents
    largest = max((p.gallons for p in prop.pools if p.active), default=0)
    tiers = session.scalars(
        select(models.RateTier).order_by(models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)
    ).all()
    for t in tiers:
        if t.max_gallons is None or largest <= t.max_gallons:
            return t.rate_cents
    return 0

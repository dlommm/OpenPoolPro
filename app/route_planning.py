"""Local route suggestions. Estimates require no API key or address disclosure."""
import hashlib
import json
import math

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import models


def properties(session, weekday: int, tech_id: int):
    return session.scalars(select(models.Property).join(models.Customer).where(
        models.Property.active, models.Customer.active,
        models.Property.weekday == weekday, models.Property.frequency != "none",
        models.Property.assigned_tech_id == (tech_id or None))
        .options(selectinload(models.Property.customer))
        .order_by(models.Property.route_order, models.Property.id)).all()


def coordinates(prop):
    lat, lon = prop.latitude, prop.longitude
    if lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def miles(a, b):
    x, y = coordinates(a), coordinates(b)
    if x is None or y is None:
        return None
    lat1, lat2 = math.radians(x[0]), math.radians(y[0])
    dlat, dlon = lat2 - lat1, math.radians(y[1] - x[1])
    hav = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 3958.8 * 2 * math.asin(math.sqrt(min(1, max(0, hav)))) * 1.3


def estimate(rows):
    legs = [miles(a, b) for a, b in zip(rows, rows[1:])]
    if any(d is None for d in legs):
        return None
    distance = sum(legs)
    return {"miles": round(distance, 1), "minutes": round(distance / 25 * 60)}


def suggest(rows):
    # Keep the first stop (the dispatcher's chosen starting point) fixed.
    # Unknown locations remain in their original slots.
    known = [p for p in rows if coordinates(p)]
    if len(known) < 3:
        return list(rows)
    route, remaining = [known[0]], known[1:]
    while remaining:
        next_ = min(remaining, key=lambda p: (miles(route[-1], p), p.id))
        route.append(next_)
        remaining.remove(next_)
    # Don't offer a worse known-stop route than the current order.
    if estimate(route)["miles"] > estimate(known)["miles"]:
        route = known
    it = iter(route)
    return [next(it) if coordinates(p) else p for p in rows]


def fingerprint(rows):
    values = [(p.id, p.route_order, p.assigned_tech_id, p.weekday,
               p.latitude, p.longitude) for p in rows]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()

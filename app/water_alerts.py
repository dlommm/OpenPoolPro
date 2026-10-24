"""Explainable review signals derived from configured targets, not dosing advice."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import chem_targets, models, water_history


def current(session, today):
    visits = session.scalars(select(models.Visit).join(models.Property).join(models.Customer).where(
        models.Visit.status == "done", models.Visit.date >= today - timedelta(days=90),
        models.Visit.date <= today, models.Property.active, models.Customer.active)
        .options(selectinload(models.Visit.property).selectinload(models.Property.customer),
                 selectinload(models.Visit.property).selectinload(models.Property.pools))
        .order_by(models.Visit.date.desc(), models.Visit.id.desc())).all()
    histories = defaultdict(list)
    for visit in visits:
        for metric, *_ in water_history.METRICS:
            value = getattr(visit, metric)
            if value is not None and math.isfinite(value) and len(histories[visit.property_id, metric]) < 3:
                histories[visit.property_id, metric].append((visit, value))
    targets, bands = chem_targets.current(session)
    reviews = {r.fingerprint: r.job_id for r in session.scalars(select(models.WaterReview)).all()}
    alerts = []
    labels = {key: label for key, label, *_ in water_history.METRICS}
    for (pid, metric), points in histories.items():
        target, band = targets[metric], bands[metric]
        if len(points) < 2 or not math.isfinite(target):
            continue
        prop = points[0][0].property
        if metric == "salt" and not any(p.active and p.water_type == "salt" for p in prop.pools):
            continue
        values = [v for _, v in points]
        def side(v, target=target, band=band):
            return 1 if v > target + band else -1 if v < target - band else 0
        repeated = side(values[0]) != 0 and side(values[0]) == side(values[1])
        drifting = (len(values) == 3 and side(values[0]) != 0
                    and abs(values[0] - target) > abs(values[1] - target) > abs(values[2] - target))
        if not (repeated or drifting):
            continue
        reason = "Repeated readings outside target" if repeated else "Readings moving away from target"
        fingerprint = hashlib.sha256(json.dumps([pid, metric, target,
            [(v.id, value) for v, value in points]]).encode()).hexdigest()
        alerts.append({"property": prop, "metric": metric, "label": labels[metric], "reason": reason,
            "values": list(reversed(values)), "date": points[0][0].date,
            "low": target - band, "high": target + band, "fingerprint": fingerprint,
            "job_id": reviews.get(fingerprint)})
    return sorted(alerts, key=lambda a: (a["property"].customer.name, a["label"]))

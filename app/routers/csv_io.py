import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session
from app.deps import require_admin
from app.main import templates

router = APIRouter(dependencies=[Depends(require_admin)])

EXPORT_COLUMNS = ["customer_name", "qb_display_name", "phone", "email", "customer_notes",
                  "property_label", "address", "gate_code", "dog_note", "frequency",
                  "weekday", "rate_override_dollars", "assigned_tech", "pool_shape",
                  "length_ft", "width_ft", "avg_depth_ft", "water_type"]


@router.get("/customers/export")
def export_customers(session: Session = Depends(get_session)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    props = session.scalars(select(models.Property).join(models.Customer).where(
        models.Property.active, models.Customer.active)
        .order_by(models.Customer.name)).all()
    for p in props:
        c = p.customer
        pool = next((x for x in p.pools if x.active), None)
        tech = session.get(models.User, p.assigned_tech_id) if p.assigned_tech_id else None
        w.writerow([
            c.name, c.qb_display_name, c.phone, c.email, c.notes,
            p.label, p.address, p.gate_code, p.dog_note, p.frequency, p.weekday,
            f"{p.rate_override_cents / 100:.2f}" if p.rate_override_cents is not None else "",
            tech.username if tech else "",
            pool.shape if pool else "", pool.length_ft if pool else "",
            (pool.width_ft if pool.width_ft is not None else "") if pool else "",
            pool.avg_depth_ft if pool else "", pool.water_type if pool else "",
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=customers.csv"})

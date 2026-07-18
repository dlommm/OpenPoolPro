import calendar
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models


@dataclass
class CustomerMonth:
    customer: models.Customer
    labor_cents: int = 0
    labor_visits: int = 0
    chem_cost_cents: int = 0
    chem_billable_cents: int = 0
    jobs: list = field(default_factory=list)
    job_cents: int = 0
    total_cents: int = 0


def _month_range(year: int, month: int) -> tuple[date, date]:
    return (date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]))


def _local_date(dt: datetime) -> date:
    """Business-local calendar date for a naive-UTC timestamp."""
    return dt.replace(tzinfo=timezone.utc).astimezone(config.tz()).date()


def month_rollup(session: Session, year: int, month: int) -> list[CustomerMonth]:
    first, last = _month_range(year, month)
    markup = float(models.get_setting(session, "chem_markup_percent", "0"))
    out: dict[int, CustomerMonth] = {}

    def row(cust: models.Customer) -> CustomerMonth:
        return out.setdefault(cust.id, CustomerMonth(customer=cust))

    visits = session.scalars(select(models.Visit).where(
        models.Visit.status == "done",
        models.Visit.date >= first, models.Visit.date <= last)).all()
    for v in visits:
        r = row(v.property.customer)
        r.labor_cents += v.rate_cents or 0
        r.labor_visits += 1
        r.chem_cost_cents += sum(vc.total_cost_cents for vc in v.chemicals)

    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "done",
        models.Job.completed_at.is_not(None),
        models.Job.completed_at >= datetime.combine(first - timedelta(days=1), time.min),
        models.Job.completed_at <= datetime.combine(last + timedelta(days=1), time.max))).all()
    for j in jobs:
        if first <= _local_date(j.completed_at) <= last:
            r = row(j.property.customer)
            r.jobs.append(j)
            r.job_cents += j.labor_cents

    rows = []
    for r in out.values():
        r.chem_billable_cents = round(r.chem_cost_cents * (1 + markup / 100))
        r.total_cents = r.labor_cents + r.chem_billable_cents + r.job_cents
        if r.total_cents > 0:
            rows.append(r)
    rows.sort(key=lambda r: r.customer.name)
    return rows


def export_csv(session: Session, year: int, month: int) -> str:
    first, last = _month_range(year, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Customer", "Service Date", "Product/Service", "Description", "Amount"])
    for r in month_rollup(session, year, month):
        name = r.customer.qb_display_name or r.customer.name
        if r.labor_cents:
            w.writerow([name, last.isoformat(), "Pool Service — Labor",
                        f"{r.labor_visits} visits {first:%b %Y}",
                        f"{r.labor_cents / 100:.2f}"])
        if r.chem_billable_cents:
            w.writerow([name, last.isoformat(), "Pool Service — Chemicals",
                        f"Chemicals {first:%b %Y}",
                        f"{r.chem_billable_cents / 100:.2f}"])
        for j in r.jobs:
            w.writerow([name, _local_date(j.completed_at).isoformat(), "Pool Service — Job",
                        j.title, f"{j.labor_cents / 100:.2f}"])
    return buf.getvalue()

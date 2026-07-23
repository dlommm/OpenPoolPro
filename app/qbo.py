import calendar
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models, tax


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
    tax_cents: int = 0
    # Cash-excluded mirrors of the above -- these feed the QuickBooks CSV
    # export ONLY. The plain accumulators above keep including cash items so
    # the Money page (and its total_cents) is unaffected by paid_cash.
    noncash_labor_cents: int = 0
    noncash_labor_visits: int = 0
    noncash_chem_billable_cents: int = 0
    noncash_tax_cents: int = 0


def _month_range(year: int, month: int) -> tuple[date, date]:
    return (date(year, month, 1),
            date(year, month, calendar.monthrange(year, month)[1]))


def _local_date(dt: datetime) -> date:
    """Business-local calendar date for a naive-UTC timestamp."""
    return dt.replace(tzinfo=timezone.utc).astimezone(config.tz()).date()


def line_billable_cents(cost_cents: int, markup_percent: float) -> int:
    """Atomic-grain markup rounding: round per VisitChemical line, not per bucket.

    Shared by qbo.month_rollup and all app.analytics chem-billable computations
    so every screen agrees to the cent.
    """
    return round(cost_cents * (1 + markup_percent / 100))


def effective_markup(chemical, global_markup: float) -> float:
    """Per-item markup when the chemical sets one, else the global markup."""
    if chemical is not None and chemical.markup_percent is not None:
        return chemical.markup_percent
    return global_markup


def billable_for_vc(vc, global_markup: float) -> int:
    """Customer-billable cents for one VisitChemical, honouring its chemical's
    per-item markup (fallback to global). Single source of truth so qbo,
    analytics, and invoicing all agree to the cent."""
    return line_billable_cents(vc.total_cost_cents, effective_markup(vc.chemical, global_markup))


def month_rollup(session: Session, year: int, month: int) -> list[CustomerMonth]:
    first, last = _month_range(year, month)
    try:
        markup = float(models.get_setting(session, "chem_markup_percent", "0"))
    except ValueError:
        markup = 0.0
    out: dict[int, CustomerMonth] = {}
    enabled = tax.tax_enabled(session)

    def row(cust: models.Customer) -> CustomerMonth:
        return out.setdefault(cust.id, CustomerMonth(customer=cust))

    visits = session.scalars(select(models.Visit).where(
        models.Visit.status == "done",
        models.Visit.date >= first, models.Visit.date <= last)).all()
    for v in visits:
        r = row(v.property.customer)
        labor = tax.effective_labor_cents(v)
        r.labor_cents += labor
        r.labor_visits += 1
        visit_chem_billable = 0
        for vc in v.chemicals:
            r.chem_cost_cents += vc.total_cost_cents
            billable = billable_for_vc(vc, markup)
            r.chem_billable_cents += billable
            visit_chem_billable += billable
        visit_tax = tax.visit_breakdown(session, v)["tax"] if enabled else 0
        if enabled:
            r.tax_cents += visit_tax
        if not v.paid_cash:
            r.noncash_labor_cents += labor
            r.noncash_labor_visits += 1
            r.noncash_chem_billable_cents += visit_chem_billable
            if enabled:
                r.noncash_tax_cents += visit_tax

    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "done",
        models.Job.completed_at.is_not(None),
        models.Job.completed_at >= datetime.combine(first - timedelta(days=1), time.min),
        models.Job.completed_at <= datetime.combine(last + timedelta(days=1), time.max))).all()
    for j in jobs:
        if first <= _local_date(j.completed_at) <= last:
            r = row(j.property.customer)
            r.jobs.append(j)
            job_labor = tax.effective_labor_cents(j)
            r.job_cents += job_labor
            job_tax = tax.job_breakdown(session, j)["tax"] if enabled else 0
            if enabled:
                r.tax_cents += job_tax
            if not j.paid_cash:
                if enabled:
                    r.noncash_tax_cents += job_tax

    rows = []
    for r in out.values():
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
        # Cash-marked items are deliberately excluded from the QuickBooks
        # export (below) but still counted in r.labor_cents/chem_billable_cents/
        # job_cents/tax_cents/total_cents, which feed the Money page.
        if r.noncash_labor_cents:
            w.writerow([name, last.isoformat(), "Pool Service — Labor",
                        f"{r.noncash_labor_visits} visits {first:%b %Y}",
                        f"{r.noncash_labor_cents / 100:.2f}"])
        if r.noncash_chem_billable_cents:
            w.writerow([name, last.isoformat(), "Pool Service — Chemicals",
                        f"Chemicals {first:%b %Y}",
                        f"{r.noncash_chem_billable_cents / 100:.2f}"])
        for j in r.jobs:
            if j.paid_cash:
                continue
            w.writerow([name, _local_date(j.completed_at).isoformat(), "Pool Service — Job",
                        j.title, f"{tax.effective_labor_cents(j) / 100:.2f}"])
        if r.noncash_tax_cents:
            w.writerow([name, last.isoformat(), "Pool Service — Sales Tax",
                        f"Sales tax {first:%b %Y}", f"{r.noncash_tax_cents / 100:.2f}"])
    return buf.getvalue()

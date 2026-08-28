"""Invoice numbering + per-customer billing data assembly.

Pure/testable (no FastAPI dependency). `invoice_data` mirrors the same
queries `qbo.month_rollup` uses (same status/date filters, same
`qbo.line_billable_cents` atomic-line chem rounding) so a customer's
invoice total always reconciles to the cent with the Money screen's
rollup for that customer/period. Never recompute frozen billing values —
`Visit.rate_cents`, `VisitChemical.total_cost_cents`, and `Job.labor_cents`
are read as-stored.
"""
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config, models, qbo, receivables, tax
from app.company import company_info
from app.timefmt import to_local_hhmm


@dataclass
class VisitLine:
    date: date
    label: str
    duration_minutes: int | None
    rate_cents: int


@dataclass
class ChemLine:
    name: str
    quantity: float
    unit: str
    billable_cents: int


@dataclass
class JobLine:
    title: str
    date: date
    times_display: str
    labor_cents: int


@dataclass
class InvoiceData:
    company: dict
    customer_name: str
    qb_name: str
    address: str
    number: str | None
    period: str
    visits: list[VisitLine] = field(default_factory=list)
    chems: list[ChemLine] = field(default_factory=list)
    jobs: list[JobLine] = field(default_factory=list)
    subtotal_cents: int = 0
    tax_cents: int = 0
    total_cents: int = 0
    generated: datetime | None = None


def next_number(session: Session, year: int) -> str:
    """Next `INV-{year}-{NNNN}` — max existing suffix for that year + 1."""
    prefix = f"INV-{year}-"
    numbers = session.scalars(select(models.Invoice.number).where(
        models.Invoice.number.like(f"{prefix}%"))).all()
    max_n = 0
    for n in numbers:
        try:
            max_n = max(max_n, int(n[len(prefix):]))
        except ValueError:
            continue
    return f"{prefix}{max_n + 1:04d}"


def invoice_data(session: Session, customer: models.Customer, year: int, month: int) -> InvoiceData:
    """Assemble one customer's invoice line data for a billing period.

    Reuses the exact bucketing rules from app.qbo.month_rollup (visits done
    in month by Visit.date; jobs by business-local completed date via a
    coarse ±1-day SQL bound + exact qbo._local_date filter; chem lines
    atomically rounded per VisitChemical line) so total_cents always equals
    that customer's month_rollup row total.
    """
    first, last = qbo._month_range(year, month)
    try:
        markup = float(models.get_setting(session, "chem_markup_percent", "0"))
    except ValueError:
        markup = 0.0

    properties = sorted(customer.properties, key=lambda p: p.id)
    property_ids = [p.id for p in properties]
    address = properties[0].address if properties else ""

    visits: list[VisitLine] = []
    chem_totals: dict[int, list] = {}  # chemical_id -> [name, unit, qty, billable_cents]
    if property_ids:
        visit_rows = session.scalars(select(models.Visit).where(
            models.Visit.property_id.in_(property_ids),
            models.Visit.status == "done",
            models.Visit.date >= first, models.Visit.date <= last)).all()
        for v in visit_rows:
            visits.append(VisitLine(date=v.date, label=v.property.label,
                                    duration_minutes=v.duration_minutes,
                                    rate_cents=tax.effective_labor_cents(v)))
            for vc in v.chemicals:
                billable = qbo.billable_for_vc(vc, markup)
                bucket = chem_totals.setdefault(
                    vc.chemical_id, [vc.chemical.name, vc.chemical.unit, 0.0, 0])
                bucket[2] += vc.amount
                bucket[3] += billable
    visits.sort(key=lambda vl: vl.date)

    chems = [ChemLine(name=name, quantity=qty, unit=unit, billable_cents=billable)
             for name, unit, qty, billable in chem_totals.values()]
    chems.sort(key=lambda cl: cl.name)

    jobs: list[JobLine] = []
    if property_ids:
        job_rows = session.scalars(select(models.Job).where(
            models.Job.property_id.in_(property_ids),
            models.Job.status == "done",
            models.Job.completed_at.is_not(None),
            models.Job.completed_at >= datetime.combine(first - timedelta(days=1), time.min),
            models.Job.completed_at <= datetime.combine(last + timedelta(days=1), time.max))).all()
        for j in job_rows:
            local_date = qbo._local_date(j.completed_at)
            if first <= local_date <= last:
                start = to_local_hhmm(j.started_at)
                end = to_local_hhmm(j.completed_at)
                times_display = f"{start}–{end}" if start and end else ""
                jobs.append(JobLine(title=j.title, date=local_date,
                                    times_display=times_display,
                                    labor_cents=tax.effective_labor_cents(j)))
    jobs.sort(key=lambda jl: jl.date)

    subtotal_cents = (sum(v.rate_cents for v in visits)
                      + sum(c.billable_cents for c in chems)
                      + sum(j.labor_cents for j in jobs))

    tax_total = 0
    if tax.tax_enabled(session):
        for v in (visit_rows if property_ids else []):
            tax_total += tax.visit_breakdown(session, v)["tax"]
        for j in (job_rows if property_ids else []):
            if first <= qbo._local_date(j.completed_at) <= last:
                tax_total += tax.job_breakdown(session, j)["tax"]

    return InvoiceData(
        company=company_info(session),
        customer_name=customer.name,
        qb_name=customer.qb_display_name or customer.name,
        address=address,
        number=None,
        period=date(year, month, 1).strftime("%B %Y"),
        visits=visits,
        chems=chems,
        jobs=jobs,
        subtotal_cents=subtotal_cents,
        tax_cents=tax_total,
        total_cents=subtotal_cents + tax_total,
        generated=models.utcnow(),
    )


def _invoices_dir() -> str:
    """PDF storage dir — mirrors the branding dir pattern in routers/company.py."""
    d = os.path.join(config.DATA_DIR, "invoices")
    os.makedirs(d, exist_ok=True)
    return d


def generate_month(session: Session, year: int, month: int, user_id: int | None) -> tuple[int, int]:
    """Generate one invoice per billable customer for a period.

    For each app.qbo.month_rollup row: skip if a non-void invoice already
    exists for (customer, period) — idempotent re-runs and a voided invoice
    both fall out of this check naturally (void doesn't count as existing,
    so re-running after a void creates a fresh invoice with a new number).
    Otherwise allocate the next number, render + write the PDF, and insert
    the Invoice row in the same commit as the number allocation so numbers
    are never reserved across a partial failure. The PDF is written to a
    `.tmp` sibling then atomically moved into place (`os.replace`) so a
    crash mid-write never leaves a truncated file at the final path.

    A concurrent generate call (e.g. two admins clicking "Generate" at
    once) can race in two ways: colliding on the Invoice.number unique
    constraint via `next_number()`, or both passing the existing-invoice
    check before either commits (which would double-bill the customer —
    with DIFFERENT numbers, so the number constraint alone can't catch it;
    the `uq_invoices_customer_period_live` partial unique index does).
    Either per-row commit failure is caught, rolled back, and counted as
    skipped rather than aborting the whole batch.

    Returns (created, skipped).
    """
    from app import invoice_pdf

    created = 0
    skipped = 0
    # Terms run from the END of the billing period, resolved once for the batch.
    _, period_end = qbo._month_range(year, month)
    net_days = receivables.default_net_days(session)
    for row in qbo.month_rollup(session, year, month):
        existing = session.scalars(select(models.Invoice).where(
            models.Invoice.customer_id == row.customer.id,
            models.Invoice.period_year == year,
            models.Invoice.period_month == month,
            models.Invoice.status != "void")).first()
        if existing is not None:
            skipped += 1
            continue

        data = invoice_data(session, row.customer, year, month)
        number = next_number(session, year)
        data.number = number
        pdf_bytes = invoice_pdf.render_invoice(data)
        filename = f"{number}.pdf"

        session.add(models.Invoice(
            number=number, customer_id=row.customer.id,
            period_year=year, period_month=month,
            subtotal_cents=data.subtotal_cents, tax_cents=data.tax_cents,
            total_cents=data.total_cents, status="draft",
            # Dated from the end of the billing period, not from whenever the
            # owner got around to generating — otherwise a late run quietly
            # grants the customer extra days and the aging report understates.
            due_date=period_end + timedelta(days=net_days),
            pdf_filename=filename))
        try:
            session.commit()
        except IntegrityError:
            # Lost a concurrent-generate race: either next_number() collided
            # or the other run already committed this (customer, period). Commit
            # BEFORE writing the PDF (unlike before) so the loser never
            # overwrites the winner's file at the shared INV-YYYY-NNNN.pdf path
            # — invoice_regenerate writes-after-commit for this same reason.
            session.rollback()
            skipped += 1
            continue

        path = os.path.join(_invoices_dir(), filename)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp_path, path)
        created += 1

    return created, skipped

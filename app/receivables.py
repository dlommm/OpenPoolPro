"""Accounts receivable: what is owed, by whom, and how late.

Pure query/aggregation layer — no FastAPI here, so the numbers can be tested
directly. Everything is derived from Invoice.total_cents minus recorded
Payment rows; nothing about "paid" is stored on the invoice, so this can never
disagree with the payments actually entered.
"""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import models

# Upper bound (inclusive) of each aging bucket, in days past the due date.
# None is the catch-all. Matches the buckets every accountant expects.
AGING_BUCKETS = (
    ("Current", 0),
    ("1-30", 30),
    ("31-60", 60),
    ("61-90", 90),
    ("90+", None),
)


def bucket_for(days_overdue: int) -> str:
    """Bucket label for a number of days past due. 0 (or negative) is Current."""
    if days_overdue <= 0:
        return "Current"
    for label, upper in AGING_BUCKETS:
        if upper is not None and days_overdue <= upper:
            return label
    return "90+"


@dataclass
class CustomerAR:
    customer: "models.Customer"
    invoices: list["models.Invoice"] = field(default_factory=list)
    balance_cents: int = 0
    oldest_days: int = 0
    buckets: dict[str, int] = field(default_factory=dict)


def open_invoices(session: Session) -> list["models.Invoice"]:
    """Every non-void invoice with an outstanding balance, oldest due first.

    A draft invoice is deliberately included: the work is done and the money is
    owed whether or not the paperwork has been emailed yet — an AR report that
    only counted 'sent' would under-report what the business is owed.
    """
    invoices = session.scalars(
        select(models.Invoice)
        .where(models.Invoice.status != "void")
        .options(selectinload(models.Invoice.payments),
                 selectinload(models.Invoice.customer))).all()
    open_ = [inv for inv in invoices if inv.balance_cents > 0]
    # Nulls last so an invoice with no due date does not sort ahead of real
    # overdue ones; then by number for a stable order within a due date.
    return sorted(open_, key=lambda i: (i.due_date is None, i.due_date or date.max, i.number))


def by_customer(session: Session, today: date) -> list[CustomerAR]:
    """Open balance per customer, most owed first."""
    rows: dict[int, CustomerAR] = {}
    for inv in open_invoices(session):
        row = rows.get(inv.customer_id)
        if row is None:
            row = rows[inv.customer_id] = CustomerAR(
                customer=inv.customer, buckets={label: 0 for label, _ in AGING_BUCKETS})
        overdue = inv.days_overdue(today)
        row.invoices.append(inv)
        row.balance_cents += inv.balance_cents
        row.oldest_days = max(row.oldest_days, overdue)
        row.buckets[bucket_for(overdue)] += inv.balance_cents
    return sorted(rows.values(), key=lambda r: r.balance_cents, reverse=True)


def totals(rows: list[CustomerAR]) -> dict:
    """Grand total plus a per-bucket summary for the report header."""
    buckets = {label: 0 for label, _ in AGING_BUCKETS}
    for row in rows:
        for label, cents in row.buckets.items():
            buckets[label] += cents
    return {"total_cents": sum(r.balance_cents for r in rows),
            "buckets": buckets,
            "customer_count": len(rows)}


def default_net_days(session: Session) -> int:
    """Payment terms in days, used to stamp a due date at generation time."""
    try:
        return max(0, int(models.get_setting(session, "invoice_net_days", "15") or 15))
    except (TypeError, ValueError):
        return 15


def record_payment(session: Session, invoice: "models.Invoice", *, amount_cents: int,
                   received_on: date, method: str, reference: str = "",
                   note: str = "", user_id: int | None = None) -> "models.Payment | None":
    """Log money received. Returns None (mutating nothing) on invalid input.

    Overpayment is allowed — customers really do pay a round number across two
    invoices — but a non-positive amount is always a mistake, and recording a
    payment against a void invoice would resurrect a balance that was written
    off on purpose.
    """
    if amount_cents <= 0 or invoice.status == "void":
        return None
    payment = models.Payment(
        invoice_id=invoice.id, amount_cents=amount_cents, received_on=received_on,
        method=method if method in models.PAYMENT_METHODS else "other",
        reference=reference.strip()[:60], note=note.strip()[:200], user_id=user_id)
    session.add(payment)
    return payment

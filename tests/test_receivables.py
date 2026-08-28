"""Payments and accounts receivable.

Paid-ness is DERIVED from Payment rows rather than stored on the invoice, so
these tests pin that the derivation is right and that nothing can produce a
balance that disagrees with the money recorded.
"""
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app import models, receivables


def _invoice(engine, *, total=10000, due=None, status="draft", number="INV-2026-0001"):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True, email="acme@example.com")
        s.add(c)
        s.flush()
        inv = models.Invoice(number=number, customer_id=c.id, period_year=2026,
                             period_month=6, total_cents=total, tax_cents=0,
                             status=status, due_date=due, pdf_filename=f"{number}.pdf")
        s.add(inv)
        s.commit()
        return inv.id, c.id


# ------------------------------------------------------------- derivation ----

def test_a_fresh_invoice_is_unpaid_and_owes_its_total(engine):
    iid, _ = _invoice(engine, total=10000)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.amount_paid_cents == 0
        assert inv.balance_cents == 10000
        assert inv.payment_state == "unpaid"


def test_partial_then_full_payment(engine):
    iid, _ = _invoice(engine, total=10000)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        receivables.record_payment(s, inv, amount_cents=4000,
                                   received_on=date(2026, 7, 1), method="check")
        s.commit()
        s.refresh(inv)
        assert inv.balance_cents == 6000 and inv.payment_state == "partial"

        receivables.record_payment(s, inv, amount_cents=6000,
                                   received_on=date(2026, 7, 8), method="cash")
        s.commit()
        s.refresh(inv)
        assert inv.balance_cents == 0 and inv.payment_state == "paid"


def test_overpayment_is_allowed_and_never_goes_negative_on_the_report(engine):
    """A customer paying one round number across two invoices is normal."""
    iid, _ = _invoice(engine, total=10000)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        receivables.record_payment(s, inv, amount_cents=15000,
                                   received_on=date(2026, 7, 1), method="check")
        s.commit()
        s.refresh(inv)
        assert inv.payment_state == "paid"
        assert inv.balance_cents == -5000        # the credit is visible...
        assert receivables.open_invoices(s) == []  # ...but it is not receivable


def test_zero_and_negative_payments_are_rejected(engine):
    iid, _ = _invoice(engine)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert receivables.record_payment(s, inv, amount_cents=0,
                                          received_on=date.today(), method="check") is None
        assert receivables.record_payment(s, inv, amount_cents=-500,
                                          received_on=date.today(), method="check") is None
        s.commit()
        s.refresh(inv)
        assert inv.amount_paid_cents == 0


def test_void_invoices_owe_nothing_and_refuse_payment(engine):
    iid, _ = _invoice(engine, total=10000, status="void")
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.balance_cents == 0 and inv.payment_state == "void"
        assert receivables.record_payment(s, inv, amount_cents=1000,
                                          received_on=date.today(), method="check") is None
        assert receivables.open_invoices(s) == []


def test_an_unknown_method_falls_back_rather_than_raising(engine):
    iid, _ = _invoice(engine)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        p = receivables.record_payment(s, inv, amount_cents=100,
                                       received_on=date.today(), method="bitcoin")
        s.commit()
        assert p.method == "other"


# ------------------------------------------------------------------ aging ----

@pytest.mark.parametrize("days,expected", [
    (-5, "Current"), (0, "Current"), (1, "1-30"), (30, "1-30"),
    (31, "31-60"), (60, "31-60"), (61, "61-90"), (90, "61-90"),
    (91, "90+"), (365, "90+"),
])
def test_bucket_boundaries(days, expected):
    assert receivables.bucket_for(days) == expected


def test_days_overdue_is_zero_until_the_due_date_passes(engine):
    today = date(2026, 8, 1)
    iid, _ = _invoice(engine, due=date(2026, 8, 15))
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).days_overdue(today) == 0


def test_days_overdue_counts_past_the_due_date(engine):
    today = date(2026, 8, 20)
    iid, _ = _invoice(engine, due=date(2026, 8, 1))
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).days_overdue(today) == 19


def test_a_paid_invoice_is_never_overdue(engine):
    today = date(2026, 9, 1)
    iid, _ = _invoice(engine, total=10000, due=date(2026, 8, 1))
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        receivables.record_payment(s, inv, amount_cents=10000,
                                   received_on=date(2026, 8, 2), method="check")
        s.commit()
        s.refresh(inv)
        assert inv.days_overdue(today) == 0


def test_an_invoice_with_no_due_date_is_never_overdue(engine):
    iid, _ = _invoice(engine, due=None)
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).days_overdue(date(2030, 1, 1)) == 0


def test_by_customer_rolls_up_and_buckets(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    today = date(2026, 9, 1)
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True)
        s.add(c)
        s.flush()
        # current, 45 days late, 120 days late — one billing period each
        # (uq_invoices_customer_period_live allows only one live invoice per
        # customer per period; the buckets only care about due_date anyway).
        for n, (total, due) in enumerate([
                (10000, date(2026, 9, 20)),
                (20000, date(2026, 7, 18)),
                (30000, date(2026, 5, 4))]):
            s.add(models.Invoice(number=f"INV-2026-000{n}", customer_id=c.id,
                                 period_year=2026, period_month=due.month,
                                 total_cents=total,
                                 tax_cents=0, status="sent", due_date=due,
                                 pdf_filename="x.pdf"))
        s.commit()

        rows = receivables.by_customer(s, today)
        assert len(rows) == 1
        row = rows[0]
        assert row.balance_cents == 60000
        assert row.oldest_days == (today - date(2026, 5, 4)).days
        assert row.buckets["Current"] == 10000
        assert row.buckets["31-60"] == 20000
        assert row.buckets["90+"] == 30000

        t = receivables.totals(rows)
        assert t["total_cents"] == 60000 and t["customer_count"] == 1
        assert sum(t["buckets"].values()) == 60000


def test_drafts_count_as_receivable(engine):
    """The work is done and the money is owed whether or not the paperwork has
    been emailed — an AR report that only counted 'sent' would under-report."""
    iid, _ = _invoice(engine, total=5000, status="draft")
    with Session(engine) as s:
        assert [i.id for i in receivables.open_invoices(s)] == [iid]


def test_net_days_setting_is_read_and_tolerant(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert receivables.default_net_days(s) == 15      # default
        models.set_setting(s, "invoice_net_days", "30")
        s.commit()
        assert receivables.default_net_days(s) == 30
        models.set_setting(s, "invoice_net_days", "nonsense")
        s.commit()
        assert receivables.default_net_days(s) == 15      # falls back, never raises

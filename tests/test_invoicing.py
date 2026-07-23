from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import invoicing, models, qbo, seeds
from tests.test_analytics import _fix


def test_next_number_sequence_and_year(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert invoicing.next_number(s, 2026) == "INV-2026-0001"
        c = models.Customer(name="N")
        s.add(c)
        s.flush()
        s.add(models.Invoice(number="INV-2026-0007", customer_id=c.id,
                             period_year=2026, period_month=7, total_cents=1,
                             pdf_filename="x.pdf"))
        s.commit()
        assert invoicing.next_number(s, 2026) == "INV-2026-0008"
        assert invoicing.next_number(s, 2027) == "INV-2027-0001"


def test_invoice_data_matches_rollup(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = {r.customer.name: r for r in qbo.month_rollup(s, 2026, 7)}
        for name, row in rows.items():
            cust = s.scalars(select(models.Customer).where(
                models.Customer.name == name)).one()
            data = invoicing.invoice_data(s, cust, 2026, 7)
            assert data.subtotal_cents == row.total_cents
            assert data.tax_cents == 0
            assert data.total_cents == data.subtotal_cents
        alpha = s.scalars(select(models.Customer).where(
            models.Customer.name == "Alpha")).one()
        d = invoicing.invoice_data(s, alpha, 2026, 7)
        assert len(d.visits) == 1 and d.visits[0].duration_minutes == 60
        assert d.visits[0].rate_cents == 5000
        assert len(d.chems) == 1 and d.chems[0].billable_cents == 1200
        assert d.period == "July 2026"
        assert any(j.labor_cents == 3000 for j in d.jobs)  # boundary job Jul 31 ET


def test_invoice_data_tax_enabled(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        models.set_setting(s, "sales_tax_enabled", "1")
        seeds.seed_tax_rates(s)
        cust = models.Customer(name="Taxed Co")
        prop = models.Property(customer=cust, state="NJ")
        s.add_all([cust, prop])
        s.flush()
        s.add(models.Visit(property_id=prop.id, date=date(2026, 7, 10),
                           status="done", rate_cents=5500))
        s.commit()

        data = invoicing.invoice_data(s, cust, 2026, 7)
        assert data.subtotal_cents == 5500
        assert data.tax_cents == 364
        assert data.total_cents == 5864


def test_invoice_data_tax_disabled(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        cust = models.Customer(name="Untaxed Co")
        prop = models.Property(customer=cust, state="NJ")
        s.add_all([cust, prop])
        s.flush()
        s.add(models.Visit(property_id=prop.id, date=date(2026, 7, 10),
                           status="done", rate_cents=5500))
        s.commit()

        data = invoicing.invoice_data(s, cust, 2026, 7)
        assert data.subtotal_cents == 5500
        assert data.tax_cents == 0
        assert data.total_cents == data.subtotal_cents


def test_invoice_model_table(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    from sqlalchemy import text
    with engine.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(invoices)"))}
    assert {"number", "customer_id", "period_year", "period_month",
            "total_cents", "status", "generated_at", "pdf_filename"} <= cols

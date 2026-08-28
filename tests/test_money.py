from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.qbo import month_rollup


def seed_month(engine):
    with Session(engine) as s:
        c = models.Customer(name="Bill Me", qb_display_name="Bill Me LLC")
        p = models.Property(customer=c)
        s.add(p)
        chem = s.scalars(select(models.Chemical)).first()
        v1 = models.Visit(property=p, date=date(2026, 7, 6), status="done",
                          rate_cents=5500, completed_at=datetime(2026, 7, 6, 10))
        v2 = models.Visit(property=p, date=date(2026, 7, 13), status="done",
                          rate_cents=5500, completed_at=datetime(2026, 7, 13, 10))
        s.add_all([v1, v2])
        s.flush()
        s.add(models.VisitChemical(visit_id=v1.id, chemical_id=chem.id, amount=2,
                                   unit_cost_cents_frozen=450, total_cost_cents=900))
        s.add(models.Job(property=p, title="Filter clean", status="done",
                         labor_cents=12500, completed_at=datetime(2026, 7, 20, 9)))
        # noise: skipped visit + other-month visit must not count
        s.add(models.Visit(property=p, date=date(2026, 7, 20), status="skipped"))
        s.add(models.Visit(property=p, date=date(2026, 6, 29), status="done",
                           rate_cents=5500))
        s.commit()


def test_month_rollup(engine):
    from app import seeds
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    seed_month(engine)
    with Session(engine) as s:
        models.set_setting(s, "chem_markup_percent", "50")
        s.commit()
        rows = month_rollup(s, 2026, 7)
        assert len(rows) == 1
        r = rows[0]
        assert r.labor_cents == 11000
        assert r.chem_cost_cents == 900
        assert r.chem_billable_cents == 1350   # 900 * 1.5
        assert r.job_cents == 12500
        assert r.total_cents == 11000 + 1350 + 12500


def test_money_page(client, engine):
    seed_month(engine)
    html = client.get("/money?ym=2026-07").text
    assert "Bill Me" in html and "$110.00" in html


def test_job_month_boundary_uses_business_tz(engine):
    from app import seeds
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        c = models.Customer(name="Boundary")
        p = models.Property(customer=c)
        s.add(p)
        # 2026-08-01 01:30 UTC == 2026-07-31 21:30 America/New_York -> July
        s.add(models.Job(property=p, title="Late job", status="done",
                         labor_cents=5000, completed_at=datetime(2026, 8, 1, 1, 30)))
        s.commit()
        july = month_rollup(s, 2026, 7)
        assert len(july) == 1 and july[0].job_cents == 5000
        assert month_rollup(s, 2026, 8) == []


def test_money_page_tolerates_bad_ym_and_year_boundaries(client, engine):
    assert client.get("/money?ym=abc").status_code == 200
    html = client.get("/money?ym=2026-01").text
    assert "/money?ym=2025-12" in html
    html = client.get("/money?ym=2026-12").text
    assert "/money?ym=2027-01" in html


def test_month_rollup_tax(engine):
    from app import seeds
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        models.set_setting(s, "sales_tax_enabled", "1")
        seeds.seed_tax_rates(s)
        c = models.Customer(name="Taxed Customer")
        p = models.Property(customer=c, state="NJ")
        s.add(p)
        s.add(models.Visit(property=p, date=date(2026, 7, 6), status="done",
                           rate_cents=5500, completed_at=datetime(2026, 7, 6, 10)))
        s.commit()
        rows = month_rollup(s, 2026, 7)
        assert len(rows) == 1
        r = rows[0]
        assert r.tax_cents == 364
        assert r.total_cents == 5500

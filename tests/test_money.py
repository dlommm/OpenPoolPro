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
    from app.db import Base
    from app import seeds
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

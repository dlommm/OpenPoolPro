"""`lifetime_revenue_cents` must agree to the cent with `revenue_by_customer`.

The customer-detail page used to compute its lifetime figure by running the
full all-time `revenue_by_customer` aggregation and throwing away every row but
one. The scoped replacement is only safe if it applies identical billing rules,
so these tests assert the two agree across the cases that actually differ:
labor overrides, chemical markup, and jobs.
"""
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app import analytics, models


def _seed(engine, *, markup="0"):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        chem = models.Chemical(name="Chlorine", unit="gal", cost_per_unit_cents=500,
                               active=True, role="chlorine_liquid", strength_pct=12.5)
        s.add(chem)
        a = models.Customer(name="Alpha", active=True)
        b = models.Customer(name="Beta", active=True)
        s.add_all([a, b])
        s.flush()
        pa = models.Property(customer_id=a.id, label="A1", active=True, weekday=0,
                             frequency="weekly", route_order=1, rate_override_cents=12000)
        pb = models.Property(customer_id=b.id, label="B1", active=True, weekday=1,
                             frequency="weekly", route_order=2, rate_override_cents=9000)
        s.add_all([pa, pb])
        s.flush()

        # Alpha: two done visits (one with a labor override), chemicals on one,
        # plus a completed job. Beta exists purely to prove scoping.
        v1 = models.Visit(property_id=pa.id, date=date(2025, 6, 2), status="done",
                          rate_cents=12000)
        v2 = models.Visit(property_id=pa.id, date=date(2025, 6, 9), status="done",
                          rate_cents=12000, labor_override_cents=5000)
        s.add_all([v1, v2])
        s.flush()
        s.add(models.VisitChemical(visit_id=v1.id, chemical_id=chem.id, amount=2.0,
                                   unit_cost_cents_frozen=500, total_cost_cents=1000))
        s.add(models.Visit(property_id=pb.id, date=date(2025, 6, 3), status="done",
                           rate_cents=9000))
        # A skipped visit must count for neither.
        s.add(models.Visit(property_id=pa.id, date=date(2025, 6, 16), status="skipped",
                           rate_cents=12000))
        s.add(models.Job(property_id=pa.id, title="Pump", status="done",
                         labor_cents=25000,
                         completed_at=datetime(2025, 6, 10, 15, 0)))
        models.set_setting(s, "chem_markup_percent", markup)
        s.commit()
        return a.id, b.id


def _via_full_aggregation(session, customer_id):
    rows = analytics.revenue_by_customer(session, date(2000, 1, 1),
                                         date.today() + timedelta(days=1))
    row = next((r for r in rows if r.customer.id == customer_id), None)
    return row.total_cents if row else 0


def test_matches_the_full_aggregation(engine):
    alpha, beta = _seed(engine)
    with Session(engine) as s:
        for cid in (alpha, beta):
            assert analytics.lifetime_revenue_cents(s, cid) == _via_full_aggregation(s, cid)


def test_matches_with_chemical_markup_applied(engine):
    alpha, _ = _seed(engine, markup="50")
    with Session(engine) as s:
        scoped = analytics.lifetime_revenue_cents(s, alpha)
        assert scoped == _via_full_aggregation(s, alpha)
        # $120 + $50 override + $25 job + chemicals at 50% markup, so the
        # markup must genuinely be in there rather than silently zero.
        assert scoped > 12000 + 5000 + 25000


def test_scoped_to_the_customer(engine):
    alpha, beta = _seed(engine)
    with Session(engine) as s:
        assert analytics.lifetime_revenue_cents(s, alpha) != \
            analytics.lifetime_revenue_cents(s, beta)


def test_customer_with_no_properties_is_zero(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = models.Customer(name="Empty", active=True)
        s.add(c)
        s.commit()
        assert analytics.lifetime_revenue_cents(s, c.id) == 0


def test_unknown_customer_is_zero(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert analytics.lifetime_revenue_cents(s, 999999) == 0

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics, models, seeds


def _fix(engine):
    """Two customers, one tech, visits across two ISO weeks + month boundary,
    chems with markup, one repair job with duration, business-tz edge job."""
    from app.db import Base
    from app.auth_core import hash_password
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        models.set_setting(s, "chem_markup_percent", "50")
        tech = models.User(name="Tech One", username="tech1", role="tech",
                           active=True, password_hash=hash_password("x" * 8))
        s.add(tech)
        a = models.Customer(name="Alpha")
        b = models.Customer(name="Beta")
        pa = models.Property(customer=a)
        pb = models.Property(customer=b)
        s.add_all([pa, pb])
        s.flush()
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        # Week 1 (Mon 2026-07-06): two visits, one attributed to tech
        v1 = models.Visit(property_id=pa.id, date=date(2026, 7, 6), status="done",
                          rate_cents=5000, completed_by_id=tech.id,
                          started_at=datetime(2026, 7, 6, 13, 0),
                          completed_at=datetime(2026, 7, 6, 14, 0))
        v2 = models.Visit(property_id=pb.id, date=date(2026, 7, 8), status="done",
                          rate_cents=7000)
        s.add_all([v1, v2])
        s.flush()
        s.add(models.VisitChemical(visit_id=v1.id, chemical_id=chem.id, amount=2,
                                   unit_cost_cents_frozen=400, total_cost_cents=800))
        # Week 2 (Mon 2026-07-13): one skipped visit + repair job by tech, 90 min
        s.add(models.Visit(property_id=pa.id, date=date(2026, 7, 14), status="skipped"))
        s.add(models.Job(property_id=pb.id, title="Pump swap", status="done",
                         job_type="repair", labor_cents=20000, completed_by_id=tech.id,
                         started_at=datetime(2026, 7, 15, 13, 0),
                         completed_at=datetime(2026, 7, 15, 14, 30)))
        # Month-boundary job: 2026-08-01 01:30 UTC == Jul 31 ET -> counts in July range
        s.add(models.Job(property_id=pa.id, title="Late July", status="done",
                         job_type="service", labor_cents=3000,
                         completed_at=datetime(2026, 8, 1, 1, 30)))
        s.commit()
        return tech.id


RANGE = (date(2026, 7, 1), date(2026, 7, 31))


def test_income_by_week(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = analytics.income_by_week(s, *RANGE)
    by_week = {r.iso_week: r for r in rows}
    w28, w29 = by_week[28], by_week[29]
    assert w28.labor_cents == 12000            # 5000 + 7000
    assert w28.chem_cost_cents == 800
    assert w28.chem_billable_cents == 1200     # 50% markup
    assert w28.total_cents == 12000 + 1200
    assert w29.job_cents == 20000
    # boundary job (Jul 31 ET) in week 31
    assert by_week[31].job_cents == 3000
    assert sum(r.total_cents for r in rows) == 12000 + 1200 + 20000 + 3000


def test_revenue_by_customer(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = analytics.revenue_by_customer(s, *RANGE)
    assert rows[0].customer.name == "Beta"      # 7000 + 20000 job
    assert rows[0].total_cents == 27000
    alpha = rows[1]
    assert alpha.total_cents == 5000 + 1200 + 3000
    assert alpha.visit_count == 1


def test_revenue_by_tech_and_unattributed(engine):
    tech_id = _fix(engine)
    with Session(engine) as s:
        rows = analytics.revenue_by_tech(s, *RANGE)
    tech_row = next(r for r in rows if r.user is not None and r.user.id == tech_id)
    unatt = next(r for r in rows if r.user is None)
    assert tech_row.total_cents == 5000 + 1200 + 20000
    assert tech_row.total_minutes == 60 + 90
    assert tech_row.revenue_per_hour_cents == round((5000 + 1200 + 20000) / 2.5)
    assert unatt.total_cents == 7000 + 3000
    assert unatt.revenue_per_hour_cents is None


def test_revenue_by_job_type(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = {r.label: r for r in analytics.revenue_by_job_type(s, *RANGE)}
    assert rows["Visits"].total_cents == 12000 + 1200
    assert rows["Repairs"].total_cents == 20000
    assert rows["Service jobs"].total_cents == 3000


def test_chem_margin(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = analytics.chem_margin(s, *RANGE)
    r = next(x for x in rows if "chlorine" in x.chemical.name.lower())
    assert r.quantity == 2 and r.cost_cents == 800
    assert r.billable_cents == 1200 and r.margin_cents == 400


def test_visit_volume(engine):
    _fix(engine)
    with Session(engine) as s:
        rows = {r.iso_week: r for r in analytics.visit_volume(s, *RANGE)}
    assert rows[28].done == 2 and rows[28].skipped == 0
    assert rows[29].skipped == 1
    assert rows[28].avg_minutes == 60          # only v1 has duration


def test_markup_rounding_agrees_across_screens(engine):
    """Atomic per-line rounding: every chem-billable consumer sums
    round(line_cost * (1 + markup/100)) per VisitChemical line, so odd-cent
    lines reconcile to the same total on every screen. Two lines of 33 cents
    at 50% markup: round(33*1.5)=50 each -> 100 total everywhere (not
    round(66*1.5)=99, which is what bucket-level rounding would give)."""
    from app import qbo
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        models.set_setting(s, "chem_markup_percent", "50")
        c1 = models.Customer(name="OddA")
        c2 = models.Customer(name="OddB")
        p1 = models.Property(customer=c1)
        p2 = models.Property(customer=c2)
        s.add_all([p1, p2])
        s.flush()
        chem = s.scalars(select(models.Chemical)).first()
        for p in (p1, p2):
            v = models.Visit(property_id=p.id, date=date(2026, 7, 6), status="done",
                             rate_cents=0)
            s.add(v)
            s.flush()
            s.add(models.VisitChemical(visit_id=v.id, chemical_id=chem.id, amount=1,
                                       unit_cost_cents_frozen=33, total_cost_cents=33))
        s.commit()
        money_total = sum(r.total_cents for r in qbo.month_rollup(s, 2026, 7))
        income_total = sum(r.total_cents for r in analytics.income_by_week(
            s, date(2026, 7, 1), date(2026, 7, 31)))
        cust_total = sum(r.total_cents for r in analytics.revenue_by_customer(
            s, date(2026, 7, 1), date(2026, 7, 31)))
        types_total = sum(r.total_cents for r in analytics.revenue_by_job_type(
            s, date(2026, 7, 1), date(2026, 7, 31)))
        chem_total = sum(r.billable_cents for r in analytics.chem_margin(
            s, date(2026, 7, 1), date(2026, 7, 31)))
        assert money_total == income_total == cust_total == types_total == chem_total == 100
        # per-line: round(33*1.5)=50 twice -> 100 everywhere


def test_rate_review_flags_underpriced(engine):
    _fix(engine)
    with Session(engine) as s:
        pa = s.scalars(select(models.Property)).first()
        s.add(models.Pool(property_id=pa.id, gallons=20000))  # medium tier 5500
        pa.rate_override_cents = 4000                          # under tier
        s.commit()
        rows = analytics.rate_review(s)
        assert len(rows) == 1
        assert rows[0].property.id == pa.id
        assert rows[0].current_cents == 4000 and rows[0].tier_cents == 5500
        assert rows[0].delta_cents == 1500
        pa.rate_override_cents = 6000                          # above tier: not flagged
        s.commit()
        assert analytics.rate_review(s) == []


def test_rate_review_ignores_spa_only_property(engine):
    """A property with only a spa pool (no pool-kind pool) must never surface
    a bogus "suggested pool rate" flag -- the pool tier ladder doesn't apply
    to it. Regression test: rate_review used to take the largest gallons
    across ALL pool kinds (including spa) and feed it into the pool tier
    lookup, which could flag a spa-only property as underpriced for a pool
    rate it was never meant to have."""
    _fix(engine)
    with Session(engine) as s:
        pb = s.scalars(select(models.Property)).all()[1]
        # Big spa (larger gallons than the biggest pool tier) -- if the bug
        # were present this would push a huge bogus "pool" tier rate.
        s.add(models.Pool(property_id=pb.id, gallons=50000, kind="spa"))
        pb.rate_override_cents = 100                            # deliberately tiny
        s.commit()
        rows = analytics.rate_review(s)
        assert all(r.property.id != pb.id for r in rows)

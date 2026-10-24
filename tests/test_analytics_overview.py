"""Analytics overview (v0.7): computations and the page."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics_overview as ao
from app import models, seeds

TODAY = date(2026, 10, 24)


def _base(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)


def _chem(s):
    return s.scalars(select(models.Chemical).where(models.Chemical.name == "Liquid chlorine")).one()


def _prop(s, name, rate=5000, *, freq="weekly", tech=None, active=True, created=None):
    c = models.Customer(name=name, active=active,
                        created_at=created or datetime(2024, 1, 1, 12))
    p = models.Property(customer=c, frequency=freq, rate_override_cents=rate,
                        assigned_tech_id=tech)
    s.add(p)
    s.flush()
    return p


def _visit(s, p, d, rate=5000, chem_cost=0):
    v = models.Visit(property_id=p.id, date=d, status="done", rate_cents=rate)
    s.add(v)
    s.flush()
    if chem_cost:
        s.add(models.VisitChemical(visit_id=v.id, chemical_id=_chem(s).id, amount=1,
                                   unit_cost_cents_frozen=chem_cost, total_cost_cents=chem_cost))
    return v


# --- pure helpers -----------------------------------------------------------

def test_month_keys_cross_year_and_single():
    assert ao.month_keys(date(2026, 2, 10), 3) == [(2025, 12), (2026, 1), (2026, 2)]
    assert ao.month_keys(TODAY, 1) == [(2026, 10)]
    assert len(ao.month_keys(TODAY, 12)) == 12


@pytest.mark.parametrize("raw,expected", [("3", 3), ("12", 12), (None, 6), ("7", 6), ("x", 6)])
def test_parse_range(raw, expected):
    assert ao.parse_range(raw) == expected


def test_nice_top_and_range():
    assert ao.nice_top(0) == 3
    assert ao.nice_top(1_000_100) >= 1_000_100
    top = ao.nice_top(987_654)
    assert top % 3 == 0 and top >= 987_654
    start, step = ao.nice_range(62, 91)
    assert start <= 62 and start + 4 * step >= 91
    start, step = ao.nice_range(80, 80)  # a single rate still gets a span
    assert start + 4 * step > start


def test_partial_month_labels():
    m = ao.MonthRow(year=2026, month=10, partial_through=24)
    assert m.partial and m.axis_label == "Oct (to 24th)"
    assert "so far" in m.long
    assert ao.MonthRow(year=2026, month=9).axis_label == "Sep"
    assert ao.ordinal(1) == "1st" and ao.ordinal(12) == "12th" and ao.ordinal(23) == "23rd"


def _row(y, m, cents, partial=None):
    return ao.MonthRow(year=y, month=m, service_cents=cents, partial_through=partial)


def test_like_for_like_compares_full_months_only():
    window = [_row(2026, 8, 10000), _row(2026, 9, 10000), _row(2026, 10, 99999, partial=24)]
    before = [_row(2026, 6, 8000), _row(2026, 7, 8000)]
    d = ao.like_for_like(window, before)
    assert d.current_cents == 20000 and d.previous_cents == 16000  # October excluded
    assert d.pct == pytest.approx(25.0) and d.up
    assert d.current_span == "Aug–Sep 2026" and d.previous_span == "Jun–Jul 2026"


def test_like_for_like_none_without_fair_comparison():
    partial_only = [_row(2026, 10, 5000, partial=24)]
    assert ao.like_for_like(partial_only, [_row(2026, 9, 100)]) is None  # single month
    window = [_row(2026, 9, 100), _row(2026, 10, 100, partial=24)]
    assert ao.like_for_like(window, []) is None                       # no history
    assert ao.like_for_like(window, [_row(2026, 8, 0)]) is None        # history is empty
    down = ao.like_for_like(window, [_row(2026, 8, 200)])
    assert down.pct == pytest.approx(-50.0) and not down.up


def test_income_mix_empty_and_shares():
    assert all(x.cents == 0 and x.share == 0 for x in ao.income_mix([]))
    mix = ao.income_mix([ao.MonthRow(2026, 9, service_cents=600, chem_cents=300, jobs_cents=100)])
    assert [x.share for x in mix] == [0.6, 0.3, 0.1]


# --- database-backed --------------------------------------------------------

def test_monthly_revenue_splits_and_marks_partial(engine):
    _base(engine)
    with Session(engine) as s:
        models.set_setting(s, "chem_markup_percent", "50")
        p = _prop(s, "Alpha")
        _visit(s, p, date(2026, 9, 3), rate=5000, chem_cost=400)
        _visit(s, p, date(2026, 10, 2), rate=5000)
        s.add(models.Job(property_id=p.id, title="Pump", status="done", job_type="repair",
                         labor_cents=20000, completed_at=datetime(2026, 9, 20, 15)))
        s.commit()
        rows = ao.monthly_revenue(s, [(2026, 8), (2026, 9), (2026, 10)], TODAY)
    aug, sep, oct_ = rows
    assert aug.total_cents == 0 and aug.visits == 0 and not aug.partial
    assert (sep.service_cents, sep.chem_cents, sep.jobs_cents, sep.visits) == (5000, 600, 20000, 1)
    assert oct_.partial and oct_.partial_through == 24 and oct_.total_cents == 5000


def test_build_on_empty_database(engine):
    _base(engine)
    with Session(engine) as s:
        ov = ao.build(s, 6, today=TODAY)
    assert len(ov.months) == 6 and ov.months[-1].partial
    assert ov.revenue_cents == 0 and ov.visits == 0 and ov.per_visit_cents == 0
    assert ov.delta is None and ov.best_full_month is None
    assert ov.rates == [] and ov.routes == [] and ov.ar_total_cents == 0
    assert ov.visits_per_full_month == 0


def test_build_delta_uses_months_before_the_window(engine):
    _base(engine)
    with Session(engine) as s:
        p = _prop(s, "Alpha")
        _visit(s, p, date(2026, 6, 10), rate=4000)    # before the 3-month window
        _visit(s, p, date(2026, 7, 10), rate=4000)
        _visit(s, p, date(2026, 8, 10), rate=5000)    # window full months
        _visit(s, p, date(2026, 9, 10), rate=5000)
        _visit(s, p, date(2026, 10, 10), rate=90000)  # partial: never compared
        s.commit()
        ov = ao.build(s, 3, today=TODAY)
    assert [m.short for m in ov.months] == ["Aug", "Sep", "Oct"]
    assert ov.revenue_cents == 100000
    assert ov.delta.current_cents == 10000 and ov.delta.previous_cents == 8000
    assert ov.best_full_month.month in (8, 9)


def test_recurring_by_route_weekly_units_and_unassigned(engine):
    _base(engine)
    with Session(engine) as s:
        tech = models.User(name="Sam Okafor", role="tech", username="sam", active=True)
        s.add(tech)
        s.flush()
        _prop(s, "Weekly", 8000, tech=tech.id)
        _prop(s, "Biweekly", 8000, freq="biweekly", tech=tech.id)
        _prop(s, "Nobody", 6000)
        _prop(s, "Paused", 9000, freq="none", tech=tech.id)
        _prop(s, "Gone", 9000, tech=tech.id, active=False)
        s.commit()
        rows = ao.recurring_by_route(s)
    assert [r.name for r in rows] == ["Sam Okafor", "Unassigned"]
    assert rows[0].weekly_cents == 8000 + 4000 and rows[0].customers == 2
    assert rows[1].unassigned and rows[1].weekly_cents == 6000


def test_receivables_by_age_buckets(engine):
    _base(engine)
    with Session(engine) as s:
        c = models.Customer(name="Owes")
        s.add(c)
        s.flush()
        for n, (due, total, paid) in enumerate([
                (TODAY + timedelta(days=5), 1000, 0),     # not due
                (TODAY - timedelta(days=10), 2000, 500),  # 1-30, part paid
                (TODAY - timedelta(days=45), 3000, 0),    # 31-60
                (TODAY - timedelta(days=120), 4000, 0),   # 90+
                (TODAY - timedelta(days=50), 5000, 5000)]):  # paid: not open
            inv = models.Invoice(number=f"INV-{n}", customer_id=c.id, period_year=2026,
                                 period_month=n + 1, total_cents=total, status="sent",
                                 due_date=due, pdf_filename="x.pdf")
            s.add(inv)
            s.flush()
            if paid:
                s.add(models.Payment(invoice_id=inv.id, amount_cents=paid, received_on=TODAY))
        s.commit()
        rows = ao.receivables_by_age(s, TODAY)
    assert [(r.cents, r.count) for r in rows] == [(1000, 1), (1500, 1), (3000, 1), (0, 0), (4000, 1)]
    assert rows[0].label == "Not due yet" and rows[0].level == "" and rows[1].level == "warn"


def test_rate_points_share_and_flag(engine):
    _base(engine)
    with Session(engine) as s:
        cheap = _prop(s, "Cheap", 10000)
        costly = _prop(s, "Costly", 10000)
        _visit(s, cheap, date(2026, 9, 1), chem_cost=500)
        _visit(s, cheap, date(2026, 9, 8), chem_cost=700)
        _visit(s, costly, date(2026, 9, 1), chem_cost=1500)   # exactly 15%: flagged
        _visit(s, costly, date(2026, 3, 1), chem_cost=90000)  # outside the range
        s.commit()
        ov = ao.build(s, 3, today=TODAY)
    by = {p.name: p for p in ov.rates}
    assert by["Cheap"].chem_per_visit_cents == 600 and not by["Cheap"].flag
    assert by["Costly"].share == pytest.approx(0.15) and by["Costly"].flag
    assert ov.rates[0].name == "Costly"  # highest share first
    assert [p.name for p in ov.flagged] == ["Costly"]


def test_customer_movement_gained_and_lost(engine):
    _base(engine)
    with Session(engine) as s:
        _prop(s, "New", created=datetime(2026, 9, 15, 12))
        _prop(s, "Old", created=datetime(2023, 1, 1, 12))
        gone = _prop(s, "Gone", active=False, created=datetime(2022, 5, 1, 12))
        _visit(s, gone, date(2026, 8, 20))
        _visit(s, gone, date(2026, 6, 1))
        _prop(s, "Never served", active=False, created=datetime(2021, 1, 1, 12))  # out of range
        s.commit()
        keys = ao.month_keys(TODAY, 3)
        mv = ao.customer_movement(s, keys, TODAY)
    assert [m.name for m in mv.gained] == ["New"]
    assert [(m.name, m.on) for m in mv.lost] == [("Gone", date(2026, 8, 20))]
    assert mv.by_month == {(2026, 8): (0, 1), (2026, 9): (1, 0), (2026, 10): (0, 0)}


# --- the page ---------------------------------------------------------------

def _seed_page(engine):
    _base(engine)
    with Session(engine) as s:
        today = date.today()
        p = _prop(s, "Coral Shores Condos", 10000)
        for wk in range(1, 30):
            _visit(s, p, today - timedelta(weeks=wk), rate=10000, chem_cost=2000)
        s.commit()


def test_overview_is_the_default_view(client, engine):
    _seed_page(engine)
    r = client.get("/analytics")
    assert r.status_code == 200
    html = r.text
    assert 'aria-current="page">Overview' in html
    assert "Revenue by month" in html and "Receivables by age" in html
    assert "Rate review" in html and "Customers gained and lost" in html
    assert "Recurring service by route" in html and "Where it came from" in html
    assert "Review at 15%" in html and "Coral" in html
    assert 'role="img"' in html and "Table view" in html and "<table" in html
    assert "so it is shown but left out of comparisons" in html


@pytest.mark.parametrize("rng,months", [("3", 3), ("12", 12), ("bogus", 6)])
def test_range_filter_scopes_the_months(client, engine, rng, months):
    _seed_page(engine)
    r = client.get("/analytics", params={"range": rng})
    assert r.status_code == 200
    assert f'aria-current="true"><span class="an-last">Last </span>{months} months' in r.text
    assert r.text.count('<th scope="row" class="an-th">') >= months


def test_overview_empty_database_renders(client, engine):
    r = client.get("/analytics?range=3")
    assert r.status_code == 200
    assert "No earlier full months to compare with" in r.text
    assert "Nothing is owed right now." in r.text


def test_overview_needs_no_script(client, engine):
    _seed_page(engine)
    html = client.get("/analytics").text
    start = html.index('class="afilters"')
    end = html.index("Customers gained and lost")
    assert "<script" not in html[start:end]


def test_overview_tech_forbidden(client, engine, tech_client):
    t = tech_client(name="notadmin")
    assert t.get("/analytics?range=3").status_code == 403


def test_detail_tabs_still_link_from_overview(client, engine):
    html = client.get("/analytics").text
    for tab in ("income", "customers", "crew", "types", "chemicals", "volume", "rates"):
        assert f"/analytics?tab={tab}&" in html

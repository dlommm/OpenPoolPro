"""ScheduleWindow must not change what the scheduler decides — only how often
it asks the database.

`stops_for_date` issues ~7 queries and materializes every active property and
customer. The Week page called it 7 times and `missed_stops` 14, so one Today
render cost ~120 queries. These tests assert the windowed path returns exactly
what the per-day path did, and pin the query count so the N+1 cannot creep back.
"""
from datetime import date, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

from app import models
from app.scheduling import ScheduleWindow, missed_stops, stops_for_date


def _seed(engine, n_props=12):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        tech = models.User(name="Sam", username="sam", role="tech", active=True,
                           password_hash="x")
        s.add(tech)
        s.flush()
        for i in range(n_props):
            c = models.Customer(name=f"C{i}", active=True)
            s.add(c)
            s.flush()
            s.add(models.Property(customer_id=c.id, label=f"P{i}", active=True,
                                  weekday=i % 7, frequency="weekly",
                                  anchor_date=date(2026, 7, 6), route_order=i,
                                  assigned_tech_id=tech.id))
        s.commit()
        return tech.id


def _count_queries(engine, fn):
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return result, len(seen)


def _as_tuples(stops):
    return [(s.property.id, s.status, s.effective_tech_id, s.moved_in) for s in stops]


def test_windowed_stops_match_the_per_day_path(engine):
    _seed(engine)
    with Session(engine) as s:
        days = [date(2026, 7, 6) + timedelta(days=i) for i in range(7)]
        window = ScheduleWindow(s, days[0], days[-1])
        for d in days:
            assert _as_tuples(stops_for_date(s, d, window=window)) == \
                _as_tuples(stops_for_date(s, d)), f"window disagrees on {d}"


def test_window_respects_holds_absences_and_moves(engine):
    """The interesting cases: a hold that starts mid-window, an absence with a
    cover, and a visit moved off its original day — each must behave the same
    through the window as it does per-day."""
    from sqlalchemy import select as _sel
    tech_id = _seed(engine)
    with Session(engine) as s:
        props = s.scalars(_sel(models.Property).order_by(models.Property.id)).all()
        cover = models.User(name="Cov", username="cov", role="tech", active=True,
                            password_hash="x")
        s.add(cover)
        s.flush()
        s.add(models.ServiceHold(property_id=props[0].id, start_date=date(2026, 7, 8),
                                 resume_date=date(2026, 7, 10)))
        s.add(models.TechAbsence(user_id=tech_id, start_date=date(2026, 7, 7),
                                 end_date=date(2026, 7, 9), cover_tech_id=cover.id))
        s.add(models.Visit(property_id=props[1].id, date=date(2026, 7, 9),
                           status="due", original_date=date(2026, 7, 7)))
        s.commit()

    with Session(engine) as s:
        days = [date(2026, 7, 6) + timedelta(days=i) for i in range(7)]
        window = ScheduleWindow(s, days[0], days[-1])
        for d in days:
            assert _as_tuples(stops_for_date(s, d, window=window)) == \
                _as_tuples(stops_for_date(s, d)), f"window disagrees on {d}"


def test_windowed_missed_stops_match(engine):
    _seed(engine)
    today = date(2026, 7, 20)
    with Session(engine) as s:
        window = ScheduleWindow(s, today - timedelta(days=14), today)
        a = [(m.property.id, m.date) for m in missed_stops(s, today, window=window)]
        b = [(m.property.id, m.date) for m in missed_stops(s, today)]
        assert a == b


def test_a_week_costs_a_constant_number_of_queries(engine):
    """Seven days through one window must not scale with the number of days."""
    _seed(engine)
    days = [date(2026, 7, 6) + timedelta(days=i) for i in range(7)]

    with Session(engine) as s:
        def windowed():
            w = ScheduleWindow(s, days[0], days[-1])
            return [stops_for_date(s, d, window=w) for d in days]
        _, windowed_queries = _count_queries(engine, windowed)

    with Session(engine) as s:
        def per_day():
            return [stops_for_date(s, d) for d in days]
        _, per_day_queries = _count_queries(engine, per_day)

    assert windowed_queries < per_day_queries / 3, (
        f"window issued {windowed_queries} queries vs {per_day_queries} per-day; "
        "the prefetch is not being reused")
    # A window is a fixed set of prefetch queries regardless of span.
    assert windowed_queries <= 10, f"expected a handful of queries, got {windowed_queries}"


def test_missed_stops_lookback_is_constant_cost(engine):
    _seed(engine)
    today = date(2026, 7, 20)
    with Session(engine) as s:
        _, windowed = _count_queries(
            engine, lambda: missed_stops(s, today))
    assert windowed <= 10, (
        f"missed_stops issued {windowed} queries; it should build one window, "
        "not re-query per day of the lookback")

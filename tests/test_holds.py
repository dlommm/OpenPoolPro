from datetime import date

from sqlalchemy.orm import Session

from app import models
from app.db import Base
from app.scheduling import active_hold_map, hold_active, missed_stops, stops_for_date


def make_prop(s, freq="weekly", weekday=0, anchor=date(2026, 7, 6), order=0):
    c = models.Customer(name=f"H{order}")
    p = models.Property(customer=c, frequency=freq, weekday=weekday,
                        anchor_date=anchor, route_order=order)
    s.add(p)
    s.commit()
    return p


def test_hold_window_edges(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s)
        s.add(models.ServiceHold(property_id=p.id, start_date=date(2026, 7, 13),
                                 resume_date=date(2026, 7, 20)))
        s.commit()
        assert hold_active(s, p.id, date(2026, 7, 12)) is False   # before start: not held
        assert hold_active(s, p.id, date(2026, 7, 13)) is True    # d == start: held
        assert hold_active(s, p.id, date(2026, 7, 19)) is True    # inside window
        assert hold_active(s, p.id, date(2026, 7, 20)) is False   # d == resume: not held


def test_hold_resume_none_is_indefinite(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s)
        s.add(models.ServiceHold(property_id=p.id, start_date=date(2026, 7, 13), resume_date=None))
        s.commit()
        assert hold_active(s, p.id, date(2026, 7, 13)) is True
        assert hold_active(s, p.id, date(2027, 1, 1)) is True     # far future: still held


def test_active_hold_map_prefetches_multiple_properties(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        held = make_prop(s, order=1)
        free = make_prop(s, order=2)
        s.add(models.ServiceHold(property_id=held.id, start_date=date(2026, 7, 13), resume_date=None))
        s.commit()
        d = date(2026, 7, 13)
        m = active_hold_map(s, d)
        assert held.id in m
        assert free.id not in m


def test_stops_for_date_excludes_held_property(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        monday = date(2026, 7, 13)
        assert len(stops_for_date(s, monday)) == 1   # due before any hold
        s.add(models.ServiceHold(property_id=p.id, start_date=monday, resume_date=date(2026, 7, 20)))
        s.commit()
        assert stops_for_date(s, monday) == []        # held on the start date
        assert len(stops_for_date(s, date(2026, 7, 20))) == 1  # due again once resumed


def test_missed_stops_excludes_hold_window_but_reports_pre_hold_miss(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # Weekly Monday property, never serviced. Hold starts July 13 (a Monday)
        # and runs indefinitely, so July 13 (and every later Monday) is covered
        # by the hold and must not count as missed — but July 6, before the
        # hold started, is a genuine pre-hold miss and must still be reported.
        p = make_prop(s, "weekly", weekday=0)
        s.add(models.ServiceHold(property_id=p.id, start_date=date(2026, 7, 13), resume_date=None))
        s.commit()
        today = date(2026, 7, 15)  # Wednesday
        missed = missed_stops(s, today)
        assert len(missed) == 1
        assert missed[0].date == date(2026, 7, 6)


def test_missed_stops_hold_window_varies_per_date_in_lookback(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        # Hold only covers July 13 (resumes July 14) — a single-week pause.
        s.add(models.ServiceHold(property_id=p.id, start_date=date(2026, 7, 13),
                                 resume_date=date(2026, 7, 14)))
        s.commit()
        today = date(2026, 7, 22)  # Wednesday, after the resumed July 20 Monday
        missed = missed_stops(s, today)
        # July 20 (Monday, after resume) is the most recent unmet stop; July 13
        # was held so it is skipped, not reported.
        assert len(missed) == 1
        assert missed[0].date == date(2026, 7, 20)

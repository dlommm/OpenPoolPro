from datetime import date

from sqlalchemy.orm import Session

from app import models, seeds
from app.scheduling import is_due, missed_stops, stops_for_date


def make_prop(s, freq="weekly", weekday=0, anchor=date(2026, 7, 6), order=0):
    c = models.Customer(name=f"C{order}")
    p = models.Property(customer=c, frequency=freq, weekday=weekday,
                        anchor_date=anchor, route_order=order)
    s.add(p)
    s.commit()
    return p


def test_is_due_weekly(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        assert is_due(p, date(2026, 7, 13)) is True    # a Monday
        assert is_due(p, date(2026, 7, 14)) is False   # Tuesday


def test_is_due_biweekly_and_every4(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "biweekly", weekday=0, anchor=date(2026, 7, 6))
        assert is_due(p, date(2026, 7, 6)) is True
        assert is_due(p, date(2026, 7, 13)) is False
        assert is_due(p, date(2026, 7, 20)) is True
        q = make_prop(s, "every4weeks", weekday=0, anchor=date(2026, 7, 6), order=1)
        assert is_due(q, date(2026, 8, 3)) is True     # 4 weeks later
        assert is_due(q, date(2026, 7, 20)) is False


def test_stops_merge_and_suppression(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        monday = date(2026, 7, 13)
        stops = stops_for_date(s, monday)
        assert len(stops) == 1 and stops[0].status == "due" and stops[0].visit is None
        # move one-time to Tuesday: due row on Tue with original_date=Mon
        s.add(models.Visit(property_id=p.id, date=date(2026, 7, 14),
                           status="due", original_date=monday))
        s.commit()
        assert stops_for_date(s, monday) == []          # suppressed at origin
        tue = stops_for_date(s, date(2026, 7, 14))
        assert len(tue) == 1 and tue[0].moved_in is True


def test_missed(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        today = date(2026, 7, 15)                        # Wednesday
        missed = missed_stops(s, today)
        assert len(missed) == 1 and missed[0].date == date(2026, 7, 13)
        # skipped visit also missed until resolved
        v = models.Visit(property_id=p.id, date=date(2026, 7, 13), status="skipped")
        s.add(v)
        s.commit()
        assert len(missed_stops(s, today)) == 1
        v.resolved = True
        s.commit()
        # After resolving July 13, July 6 (the previous Monday) becomes the next unmet stop
        assert len(missed_stops(s, today)) == 1
        assert missed_stops(s, today)[0].date == date(2026, 7, 6)


def test_missed_not_hidden_by_newer_done(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        p = make_prop(s, "weekly", weekday=0)
        s.add(models.Visit(property_id=p.id, date=date(2026, 7, 6), status="skipped"))
        s.add(models.Visit(property_id=p.id, date=date(2026, 7, 13), status="done"))
        s.commit()
        missed = missed_stops(s, date(2026, 7, 15))
        assert len(missed) == 1
        assert missed[0].date == date(2026, 7, 6)

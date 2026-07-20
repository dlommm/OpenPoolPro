from datetime import date, datetime

from app.timefmt import from_local_hhmm, parse_ym, to_local_hhmm


def test_roundtrip_summer():
    # 2026-07-20 09:30 America/New_York == 13:30 UTC (EDT)
    dt = from_local_hhmm(date(2026, 7, 20), "09:30")
    assert dt == datetime(2026, 7, 20, 13, 30)
    assert to_local_hhmm(dt) == "09:30"


def test_roundtrip_winter_dst_boundary():
    # 2026-01-15 09:30 EST == 14:30 UTC
    dt = from_local_hhmm(date(2026, 1, 15), "09:30")
    assert dt == datetime(2026, 1, 15, 14, 30)
    assert to_local_hhmm(dt) == "09:30"


def test_garbage_returns_none():
    for bad in ["", "9:99", "25:00", "abc", "12", None]:
        assert from_local_hhmm(date(2026, 7, 20), bad) is None
    assert to_local_hhmm(None) == ""


def test_duration_properties():
    from app import models
    v = models.Visit(date=date(2026, 7, 20))
    assert v.duration_minutes is None
    v.started_at = datetime(2026, 7, 20, 13, 30)
    assert v.duration_minutes is None
    v.completed_at = datetime(2026, 7, 20, 14, 12)
    assert v.duration_minutes == 42
    v.completed_at = datetime(2026, 7, 20, 13, 0)  # end before start
    assert v.duration_minutes is None
    j = models.Job(title="t", started_at=datetime(2026, 7, 20, 13, 0),
                   completed_at=datetime(2026, 7, 20, 14, 5))
    assert j.duration_minutes == 65


def test_minutes_display_filter():
    from app.main import templates
    f = templates.env.filters["minutes_display"]
    assert f(None) == ""
    assert f(42) == "42 min"
    assert f(65) == "1 h 05"
    assert f(120) == "2 h 00"


def test_parse_ym_year_clamp_falls_back():
    # In-range year/month parses normally.
    assert parse_ym("2050-05") == (2050, 5)
    # Out-of-range year falls back to default, same as malformed/missing raw.
    assert parse_ym("1999-05", default=(2026, 7)) == (2026, 7)
    assert parse_ym("2101-05", default=(2026, 7)) == (2026, 7)
    # Month validation still applies alongside the new year clamp.
    assert parse_ym("2026-13", default=(2026, 7)) == (2026, 7)
    # Boundary years are inclusive.
    assert parse_ym("2000-01", default=(1, 1)) == (2000, 1)
    assert parse_ym("2100-12", default=(1, 1)) == (2100, 12)

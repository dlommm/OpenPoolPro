"""Chemistry trend history and the customer-facing service report."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import charts, models, water_history
from app.main import ALLOWED_THEMES


def _property_with_readings(engine, rows):
    """rows: list of (date, fc, ph) -> a done visit each."""
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", active=True, weekday=0,
                            frequency="weekly", route_order=1)
        s.add(p)
        s.flush()
        for d, fc, ph in rows:
            s.add(models.Visit(property_id=p.id, date=d, status="done",
                               rate_cents=10000, fc=fc, ph=ph))
        s.commit()
        return p.id


def test_series_are_oldest_first_and_skip_missing_readings(engine):
    pid = _property_with_readings(engine, [
        (date(2026, 6, 1), 2.0, 7.4),
        (date(2026, 6, 8), None, None),   # a visit with no test at all
        (date(2026, 6, 15), 3.0, 7.6),
    ])
    with Session(engine) as s:
        series = {x.key: x for x in water_history.for_property(s, pid)}
        assert [d for d, _ in series["fc"].points] == [date(2026, 6, 1), date(2026, 6, 15)]
        assert [v for _, v in series["fc"].points] == [2.0, 3.0]
        assert series["fc"].latest == 3.0


def test_only_completed_visits_count(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", active=True, weekday=0,
                            frequency="weekly", route_order=1)
        s.add(p)
        s.flush()
        s.add(models.Visit(property_id=p.id, date=date(2026, 6, 1), status="due", fc=9.0))
        s.add(models.Visit(property_id=p.id, date=date(2026, 6, 8), status="done", fc=3.0))
        s.commit()
        series = {x.key: x for x in water_history.for_property(s, p.id)}
        assert [v for _, v in series["fc"].points] == [3.0]


def test_history_is_bounded(engine):
    rows = [(date(2026, 1, 1) + timedelta(days=7 * i), 3.0, 7.5) for i in range(60)]
    pid = _property_with_readings(engine, rows)
    with Session(engine) as s:
        series = {x.key: x for x in water_history.for_property(s, pid, limit=26)}
        assert len(series["fc"].points) == 26
        # ...and it keeps the MOST RECENT ones.
        assert series["fc"].points[-1][0] == rows[-1][0]


def test_band_and_in_band_follow_the_configured_target(engine):
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 4.0, 7.5)])
    with Session(engine) as s:
        models.set_setting(s, "target_fc", "4.0")
        s.commit()
        fc = {x.key: x for x in water_history.for_property(s, pid)}["fc"]
        assert fc.target == 4.0
        assert (fc.band_lo, fc.band_hi) == (3.5, 4.5)   # the dosing deadband
        assert fc.in_band is True


def test_a_reading_outside_the_band_is_flagged(engine):
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 0.5, 7.5)])
    with Session(engine) as s:
        models.set_setting(s, "target_fc", "4.0")
        s.commit()
        fc = {x.key: x for x in water_history.for_property(s, pid)}["fc"]
        assert fc.in_band is False


def test_axis_widens_to_include_an_out_of_range_reading(engine):
    """The extreme value is exactly the one worth seeing — it must not clip."""
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 9.6)])
    with Session(engine) as s:
        ph = {x.key: x for x in water_history.for_property(s, pid)}["ph"]
        assert ph.axis_hi >= 9.6


def test_ph_axis_is_not_zero_based(engine):
    """Plotted from zero, every pH chart is a flat line."""
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 7.4),
                                           (date(2026, 6, 8), 3.0, 7.6)])
    with Session(engine) as s:
        ph = {x.key: x for x in water_history.for_property(s, pid)}["ph"]
        assert ph.axis_lo > 5.0


def test_targets_tolerate_garbage_settings(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "target_ph", "not a number")
        s.commit()
        assert water_history.targets(s)["ph"] == 7.5     # falls back, never raises


def test_chart_renders_svg_and_escapes_labels(engine):
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 7.4),
                                           (date(2026, 6, 8), 4.0, 7.5)])
    with Session(engine) as s:
        fc = {x.key: x for x in water_history.for_property(s, pid)}["fc"]
        fc.label = '<script>alert(1)</script>'
        svg = charts.reading_chart(fc)
    assert svg.startswith("<svg") and "</svg>" in svg
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_chart_handles_a_single_reading(engine):
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 7.4)])
    with Session(engine) as s:
        fc = {x.key: x for x in water_history.for_property(s, pid)}["fc"]
        assert charts.reading_chart(fc).startswith("<svg")


# ------------------------------------------------------------------ page ----

@pytest.mark.parametrize("theme", sorted(ALLOWED_THEMES))
def test_water_page_renders_in_every_theme(client, engine, theme):
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 7.4),
                                           (date(2026, 6, 8), 4.0, 7.5)])
    with Session(engine) as s:
        models.set_setting(s, "active_theme", theme)
        s.commit()
    r = client.get(f"/properties/{pid}/water")
    assert r.status_code == 200
    assert "Free chlorine" in r.text and "<svg" in r.text


def test_water_page_is_linked_from_customer_detail_in_every_theme(client, engine):
    """The link lives in a template with a fork per theme, so it is exactly the
    kind of thing that ships in one skin and silently not the others."""
    pid = _property_with_readings(engine, [(date(2026, 6, 1), 3.0, 7.4)])
    with Session(engine) as s:
        cid = s.get(models.Property, pid).customer_id
    for theme in sorted(ALLOWED_THEMES):
        with Session(engine) as s:
            models.set_setting(s, "active_theme", theme)
            s.commit()
        body = client.get(f"/customers/{cid}").text
        assert f"/properties/{pid}/water" in body, f"no link under {theme}"


def test_water_page_with_no_readings_is_not_an_error(client, engine):
    pid = _property_with_readings(engine, [])
    r = client.get(f"/properties/{pid}/water")
    assert r.status_code == 200 and "No readings yet" in r.text


# -------------------------------------------------------- service report ----

def _complete_a_visit(client, engine, **readings):
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        chem = models.Chemical(name="Liquid chlorine", unit="gal", cost_per_unit_cents=500,
                               active=True, role="chlorine_liquid", strength_pct=12.5)
        s.add(chem)
        c = models.Customer(name="Acme", active=True, email="acme@example.com",
                            notify_email=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", active=True, weekday=0,
                            frequency="weekly", route_order=1, rate_override_cents=10000)
        s.add(p)
        s.commit()
        pid, chem_id = p.id, chem.id

    client.get(f"/visit/{pid}/{today.isoformat()}")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    data = {"status": "done", "notes": "Skimmer basket cleared",
            "chem_id": str(chem_id), "chem_amount": "2", **readings}
    client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False)
    with Session(engine) as s:
        return s.scalars(select(models.EmailOutbox)).first()


def test_service_report_includes_readings_doses_and_notes(client, engine):
    row = _complete_a_visit(client, engine, fc="1.5", ph="7.8")
    assert row is not None, "no email was queued"
    body = row.body
    assert "Water test:" in body
    assert "Free chlorine: 1.5 ppm" in body and "target" in body
    assert "pH: 7.8" in body
    assert "Chemicals added:" in body and "Liquid chlorine: 2 gal" in body
    assert "Skimmer basket cleared" in body
    assert "Technician:" in body


def test_service_report_never_mentions_money(client, engine):
    """It goes out automatically on every visit; pricing belongs on the invoice."""
    row = _complete_a_visit(client, engine, fc="3.0")
    assert "$" not in row.body
    assert "100.00" not in row.body


def test_service_report_omits_empty_sections(client, engine):
    row = _complete_a_visit(client, engine)      # no readings submitted
    assert "Water test:" not in row.body
    assert "Technician:" in row.body


def test_no_email_when_the_customer_has_not_opted_in(client, engine):
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        c = models.Customer(name="Quiet", active=True, email="q@example.com",
                            notify_email=False)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", active=True, weekday=0,
                            frequency="weekly", route_order=1)
        s.add(p)
        s.commit()
        pid = p.id
    client.get(f"/visit/{pid}/{today.isoformat()}")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={"status": "done"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []

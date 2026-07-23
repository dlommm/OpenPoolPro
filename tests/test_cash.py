import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.qbo import export_csv, month_rollup


def _seed_cash_and_noncash(engine):
    """One customer paid entirely in cash (one visit), one customer paid
    normally (one visit), a third customer with one cash job + one
    non-cash job -- covers both the aggregated-visit-line path and the
    per-job-line path in export_csv -- and a fourth customer with a mix
    of cash and non-cash visits, so the CSV labor line's visit count can
    be checked against the non-cash-only count."""
    from app.db import Base
    from app import seeds
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        cash_cust = models.Customer(name="Cash Customer", qb_display_name="Cash Customer LLC")
        cash_prop = models.Property(customer=cash_cust)
        s.add(cash_prop)
        s.add(models.Visit(property=cash_prop, date=date(2026, 7, 6), status="done",
                           rate_cents=5500, completed_at=datetime(2026, 7, 6, 10),
                           paid_cash=True))

        check_cust = models.Customer(name="Check Customer", qb_display_name="Check Customer LLC")
        check_prop = models.Property(customer=check_cust)
        s.add(check_prop)
        s.add(models.Visit(property=check_prop, date=date(2026, 7, 6), status="done",
                           rate_cents=6600, completed_at=datetime(2026, 7, 6, 10),
                           paid_cash=False))

        job_cust = models.Customer(name="Job Customer", qb_display_name="Job Customer LLC")
        job_prop = models.Property(customer=job_cust)
        s.add(job_prop)
        s.add(models.Job(property=job_prop, title="Cash Job", status="done",
                         labor_cents=9900, completed_at=datetime(2026, 7, 20, 9),
                         paid_cash=True))
        s.add(models.Job(property=job_prop, title="Check Job", status="done",
                         labor_cents=7700, completed_at=datetime(2026, 7, 21, 9),
                         paid_cash=False))

        mixed_cust = models.Customer(name="Mixed Customer", qb_display_name="Mixed Customer LLC")
        mixed_prop = models.Property(customer=mixed_cust)
        s.add(mixed_prop)
        s.add(models.Visit(property=mixed_prop, date=date(2026, 7, 8), status="done",
                           rate_cents=5000, completed_at=datetime(2026, 7, 8, 10),
                           paid_cash=True))
        s.add(models.Visit(property=mixed_prop, date=date(2026, 7, 15), status="done",
                           rate_cents=5000, completed_at=datetime(2026, 7, 15, 10),
                           paid_cash=False))
        s.add(models.Visit(property=mixed_prop, date=date(2026, 7, 22), status="done",
                           rate_cents=5000, completed_at=datetime(2026, 7, 22, 10),
                           paid_cash=False))
        s.commit()


def test_export_excludes_cash_visit_and_customer_disappears(engine):
    _seed_cash_and_noncash(engine)
    with Session(engine) as s:
        got = export_csv(s, 2026, 7)
    assert "Cash Customer" not in got
    assert "55.00" not in got
    assert "Check Customer" in got
    assert "66.00" in got


def test_export_excludes_cash_job_line_but_keeps_noncash_job(engine):
    _seed_cash_and_noncash(engine)
    with Session(engine) as s:
        got = export_csv(s, 2026, 7)
    assert "Cash Job" not in got
    assert "99.00" not in got
    assert "Check Job" in got
    assert "77.00" in got


def test_month_rollup_still_includes_cash_for_money_page(engine):
    """The Money page must be unaffected: total_cents keeps the cash amount
    even though the CSV export drops it."""
    _seed_cash_and_noncash(engine)
    with Session(engine) as s:
        rows = {r.customer.name: r for r in month_rollup(s, 2026, 7)}
        cash_row = rows["Cash Customer"]
        assert cash_row.total_cents == 5500        # cash amount still counted
        assert cash_row.labor_cents == 5500
        assert cash_row.noncash_labor_cents == 0    # but excluded from the CSV mirror

        job_row = rows["Job Customer"]
        assert job_row.total_cents == 9900 + 7700   # both jobs counted for Money
        assert job_row.job_cents == 9900 + 7700


def test_csv_labor_line_visit_count_excludes_cash_visits(engine):
    """Fix 1: for a customer with a mix of cash and non-cash done visits,
    the CSV labor line's visit count must match the number of NON-cash
    visits (whose dollars are what actually appear on that line), not the
    total visit count."""
    _seed_cash_and_noncash(engine)
    with Session(engine) as s:
        rows = {r.customer.name: r for r in month_rollup(s, 2026, 7)}
        mixed_row = rows["Mixed Customer"]
        assert mixed_row.labor_visits == 3          # total visits (incl. cash)
        assert mixed_row.noncash_labor_visits == 2   # only the non-cash visits

        got = export_csv(s, 2026, 7)
    labor_line = next(ln for ln in got.splitlines() if "Mixed Customer" in ln
                       and "Labor" in ln)
    assert "2 visits Jul 2026" in labor_line
    assert "3 visits" not in labor_line
    assert "100.00" in labor_line  # 2 non-cash visits @ $50.00 each


def _seed_customer(client):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    client.post("/customers/1/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""})
    client.post("/properties/1/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})


def test_bare_tech_cannot_mark_visit_cash(client, tech_client, engine):
    _seed_customer(client)
    t = tech_client(name="baretech")
    t.get("/visit/1/2026-07-13")  # creates the visit row
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    t.post(f"/visit/{vid}/complete", data={"paid_cash": "1"})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.paid_cash is False


def test_mark_cash_tech_can_mark_visit_cash(client, tech_client, engine):
    _seed_customer(client)
    t = tech_client(name="cashtech", mark_cash=True)
    t.get("/visit/1/2026-07-13")  # creates the visit row
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    t.post(f"/visit/{vid}/complete", data={"paid_cash": "1"})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.paid_cash is True


def test_admin_can_mark_visit_cash(client):
    _seed_customer(client)
    client.get("/visit/1/2026-07-13")  # creates the visit row
    client.post("/visit/1/complete", data={"paid_cash": "1"})
    r = client.get("/visit/1/2026-07-13")
    m = re.search(r'<input[^>]*name="paid_cash"[^>]*>', r.text)
    assert m is not None, "paid_cash checkbox missing for admin (expected can_cash=True)"
    assert "checked" in m.group(0)


def test_bare_tech_cannot_mark_job_cash(client, tech_client, engine):
    _seed_customer(client)
    client.post("/jobs/new", data={"property_id": "1", "title": "Some job",
                                   "description": "", "priority": "normal",
                                   "scheduled_date": "", "job_type": "service",
                                   "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    t = tech_client(name="baretech2")
    t.post(f"/jobs/{jid}/complete", data={"paid_cash": "1"})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.paid_cash is False


def test_mark_cash_tech_can_mark_job_cash(client, tech_client, engine):
    _seed_customer(client)
    client.post("/jobs/new", data={"property_id": "1", "title": "Some job",
                                   "description": "", "priority": "normal",
                                   "scheduled_date": "", "job_type": "service",
                                   "assigned_tech_id": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    t = tech_client(name="cashtech2", mark_cash=True)
    t.post(f"/jobs/{jid}/complete", data={"paid_cash": "1"})
    with Session(engine) as s:
        j = s.get(models.Job, jid)
        assert j.paid_cash is True


def test_mark_cash_in_permission_flags():
    assert "mark_cash" in models.User.PERMISSION_FLAGS

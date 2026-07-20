from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app import models
from app.db import Base
from app.routers.customers import _timeline


def _cust_prop(s, prop_label="Home"):
    c = models.Customer(name="Cust")
    p = models.Property(customer=c, label=prop_label)
    s.add(p)
    s.commit()
    return c, p


def test_timeline_merges_all_kinds_sorted_desc(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin", role="admin")
        s.add(admin)
        s.commit()

        v = models.Visit(property_id=p.id, date=date(2026, 1, 1), status="done",
                         completed_at=datetime(2026, 1, 1, 10, 0), rate_cents=5000)
        s.add(v)
        j = models.Job(property_id=p.id, title="Pump repair", status="done",
                       completed_at=datetime(2026, 2, 1, 9, 0), labor_cents=12000)
        s.add(j)
        s.add(models.CommLog(customer_id=c.id, at=datetime(2026, 3, 1, 8, 0),
                             kind="call", note="Called about algae"))
        s.add(models.Quote(customer_id=c.id, title="New pump", created_at=datetime(2026, 4, 1, 7, 0)))
        s.commit()
        s.add(models.Photo(entity_type="visit", entity_id=v.id, filename="a.jpg",
                           thumb_filename="a_t.jpg", taken_at=datetime(2026, 1, 1, 10, 5)))
        s.commit()

        entries = _timeline(s, c, admin)
        kinds = [e["kind"] for e in entries]
        assert kinds == ["quote", "comm", "job", "photo", "visit"]
        # newest-first: each entry's `at` must be non-increasing
        ats = [e["at"] for e in entries]
        assert ats == sorted(ats, reverse=True)


def test_timeline_money_gated_without_view_money(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        tech = models.User(name="Tech", role="tech", view_money=False)
        s.add(tech)
        s.commit()
        s.add(models.Visit(property_id=p.id, date=date(2026, 1, 1), status="done",
                           completed_at=datetime(2026, 1, 1, 10, 0), rate_cents=5000))
        s.add(models.Job(property_id=p.id, title="Repair", status="done",
                         completed_at=datetime(2026, 1, 2, 9, 0), labor_cents=8000))
        s.commit()

        entries = _timeline(s, c, tech)
        assert entries
        assert all(e["money"] is None for e in entries)

        admin = models.User(name="Admin2", role="admin")
        s.add(admin)
        s.commit()
        entries_admin = _timeline(s, c, admin)
        money_by_kind = {e["kind"]: e["money"] for e in entries_admin}
        assert money_by_kind["visit"] == 5000
        assert money_by_kind["job"] == 8000


def test_timeline_excludes_due_visits_and_bare_open_jobs(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin3", role="admin")
        s.add(admin)
        s.commit()
        s.add(models.Visit(property_id=p.id, date=date(2026, 1, 1), status="due"))
        s.add(models.Job(property_id=p.id, title="Someday", status="open"))  # no timestamps at all
        s.commit()

        assert _timeline(s, c, admin) == []


def test_timeline_caps_at_200_keeping_newest(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin4", role="admin")
        s.add(admin)
        s.commit()
        for i in range(250):
            s.add(models.CommLog(customer_id=c.id, at=datetime(2026, 1, 1) + timedelta(minutes=i),
                                 kind="note", note=f"n{i}"))
        s.commit()

        entries = _timeline(s, c, admin)
        assert len(entries) == 200
        kept_notes = {e["detail"] for e in entries}
        assert kept_notes == {f"n{i}" for i in range(50, 250)}  # newest 200 of 250


def test_timeline_photo_resolves_via_pool_and_job_chain(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin5", role="admin")
        s.add(admin)
        s.commit()
        pool = models.Pool(property_id=p.id, label="Main")
        s.add(pool)
        j = models.Job(property_id=p.id, title="Fix pump", status="done",
                       completed_at=datetime(2026, 1, 5, 9, 0))
        s.add(j)
        s.commit()
        s.add(models.Photo(entity_type="pool", entity_id=pool.id, filename="p.jpg",
                           thumb_filename="p_t.jpg", tag="before",
                           taken_at=datetime(2026, 1, 2, 10, 0)))
        s.add(models.Photo(entity_type="job", entity_id=j.id, filename="j.jpg",
                           thumb_filename="j_t.jpg", tag="after",
                           taken_at=datetime(2026, 1, 6, 10, 0)))
        s.commit()

        entries = _timeline(s, c, admin)
        photo_entries = [e for e in entries if e["kind"] == "photo"]
        assert len(photo_entries) == 2
        titles = {e["title"] for e in photo_entries}
        assert "Photo — before" in titles
        assert "Photo — after" in titles


def test_timeline_quote_money_is_line_total_creation_entry_only(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin6", role="admin")
        s.add(admin)
        s.commit()
        q = models.Quote(customer_id=c.id, title="Resurface", created_at=datetime(2026, 1, 1),
                         status="sent")
        s.add(q)
        s.commit()
        s.add(models.QuoteLine(quote_id=q.id, description="Labor", amount_cents=50000, sort_order=0))
        s.add(models.QuoteLine(quote_id=q.id, description="Materials", amount_cents=20000, sort_order=1))
        s.commit()

        entries = _timeline(s, c, admin)
        quote_entries = [e for e in entries if e["kind"] == "quote"]
        assert len(quote_entries) == 1  # one creation entry, not one per status
        assert quote_entries[0]["money"] == 70000


def test_timeline_invoice_entry_appears_with_number_in_title(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin8", role="admin")
        s.add(admin)
        s.commit()
        s.add(models.Invoice(number="INV-2026-0001", customer_id=c.id,
                             period_year=2026, period_month=6, total_cents=15000,
                             status="sent", generated_at=datetime(2026, 6, 1, 8, 0),
                             pdf_filename="inv.pdf"))
        s.commit()

        entries = _timeline(s, c, admin)
        invoice_entries = [e for e in entries if e["kind"] == "invoice"]
        assert len(invoice_entries) == 1
        e = invoice_entries[0]
        assert e["title"] == "Invoice INV-2026-0001"
        assert e["detail"] == "2026-06 · sent"
        assert e["money"] == 15000
        assert e["at"] == datetime(2026, 6, 1, 8, 0)


def test_timeline_invoice_money_gated_without_view_money(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        tech = models.User(name="Tech2", role="tech", view_money=False)
        admin = models.User(name="Admin9", role="admin")
        s.add(tech)
        s.add(admin)
        s.commit()
        s.add(models.Invoice(number="INV-2026-0002", customer_id=c.id,
                             period_year=2026, period_month=7, total_cents=20000,
                             status="draft", generated_at=datetime(2026, 7, 1, 8, 0),
                             pdf_filename="inv2.pdf"))
        s.commit()

        tech_entries = [e for e in _timeline(s, c, tech) if e["kind"] == "invoice"]
        assert len(tech_entries) == 1
        assert tech_entries[0]["money"] is None

        admin_entries = [e for e in _timeline(s, c, admin) if e["kind"] == "invoice"]
        assert admin_entries[0]["money"] == 20000


def test_timeline_invoice_sorts_with_mixed_kinds_newest_first(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        admin = models.User(name="Admin10", role="admin")
        s.add(admin)
        s.commit()
        s.add(models.Visit(property_id=p.id, date=date(2026, 1, 1), status="done",
                           completed_at=datetime(2026, 1, 1, 10, 0), rate_cents=5000))
        s.add(models.CommLog(customer_id=c.id, at=datetime(2026, 2, 1, 8, 0),
                             kind="call", note="Called"))
        s.add(models.Invoice(number="INV-2026-0003", customer_id=c.id,
                             period_year=2026, period_month=3, total_cents=9900,
                             status="draft", generated_at=datetime(2026, 3, 1, 9, 0),
                             pdf_filename="inv3.pdf"))
        s.commit()

        entries = _timeline(s, c, admin)
        kinds = [e["kind"] for e in entries]
        assert kinds == ["invoice", "comm", "visit"]
        ats = [e["at"] for e in entries]
        assert ats == sorted(ats, reverse=True)


def test_timeline_no_invoice_entries_and_no_other_customers_data(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c, p = _cust_prop(s)
        other_c, other_p = _cust_prop(s, prop_label="Other")
        admin = models.User(name="Admin7", role="admin")
        s.add(admin)
        s.commit()
        s.add(models.Visit(property_id=other_p.id, date=date(2026, 1, 1), status="done",
                           completed_at=datetime(2026, 1, 1, 10, 0), rate_cents=999))
        s.add(models.CommLog(customer_id=other_c.id, kind="note", note="not mine",
                             at=datetime(2026, 1, 1)))
        s.commit()

        assert _timeline(s, c, admin) == []

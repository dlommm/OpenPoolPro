from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models
from app.db import Base

NEW_TABLES = (
    "comm_log",
    "service_holds",
    "property_tasks",
    "maintenance_rules",
    "quotes",
    "quote_lines",
    "job_templates",
)


def _cols(engine, table):
    with engine.connect() as c:
        return {r[1] for r in c.execute(text(f"PRAGMA table_info({table})"))}


def test_new_tables_created_via_create_all(engine):
    Base.metadata.create_all(engine)
    for table in NEW_TABLES:
        assert _cols(engine, table), f"{table} should exist after create_all"


def _make_customer(session, **kw):
    c = models.Customer(name=kw.pop("name", "Cust"), **kw)
    session.add(c)
    session.commit()
    return c


def _make_property(session, customer_id, **kw):
    p = models.Property(customer_id=customer_id, **kw)
    session.add(p)
    session.commit()
    return p


def test_comm_log_model(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        s.add(models.CommLog(customer_id=cust.id, kind="call", note="Talked about algae"))
        s.commit()
        row = s.scalars(select(models.CommLog)).one()
        assert row.customer_id == cust.id
        assert row.kind == "call"
        assert row.note == "Talked about algae"
        assert row.user_id is None
        assert row.at is not None


def test_service_hold_model_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        s.add(models.ServiceHold(property_id=prop.id, start_date=date(2026, 6, 1)))
        s.commit()
        row = s.scalars(select(models.ServiceHold)).one()
        assert row.property_id == prop.id
        assert row.start_date == date(2026, 6, 1)
        assert row.resume_date is None
        assert row.reason == ""
        assert row.created_at is not None


def test_property_task_model_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        s.add(models.PropertyTask(property_id=prop.id, label="Skim"))
        s.commit()
        row = s.scalars(select(models.PropertyTask)).one()
        assert row.label == "Skim"
        assert row.sort_order == 0
        assert row.active is True


def test_maintenance_rule_model_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        s.add(models.MaintenanceRule(property_id=prop.id, title="Filter clean",
                                     next_due=date(2026, 8, 1)))
        s.commit()
        row = s.scalars(select(models.MaintenanceRule)).one()
        assert row.title == "Filter clean"
        assert row.description == ""
        assert row.job_type == "service"
        assert row.interval_weeks == 4
        assert row.lead_days == 7
        assert row.next_due == date(2026, 8, 1)
        assert row.active is True


def test_quote_and_lines_relationship_ordered(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        quote = models.Quote(customer_id=cust.id, title="Repair estimate")
        s.add(quote)
        s.commit()
        s.add_all([
            models.QuoteLine(quote_id=quote.id, description="Second", amount_cents=500, sort_order=2),
            models.QuoteLine(quote_id=quote.id, description="First", amount_cents=1000, sort_order=1),
        ])
        s.commit()
        row = s.scalars(select(models.Quote)).one()
        assert row.status == "draft"
        assert row.notes == ""
        assert row.property_id is None
        assert row.user_id is None
        assert row.job_id is None
        assert row.created_at is not None
        assert [line.description for line in row.lines] == ["First", "Second"]


def test_quote_line_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        quote = models.Quote(customer_id=cust.id, title="Q")
        s.add(quote)
        s.commit()
        s.add(models.QuoteLine(quote_id=quote.id, description="Labor", amount_cents=2500))
        s.commit()
        line = s.scalars(select(models.QuoteLine)).one()
        assert line.sort_order == 0
        assert line.quote_id == quote.id


def test_job_template_model_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.JobTemplate(title="Pump replacement"))
        s.commit()
        row = s.scalars(select(models.JobTemplate)).one()
        assert row.title == "Pump replacement"
        assert row.description == ""
        assert row.job_type == "service"
        assert row.labor_cents == 0


def test_customer_tags_column_default(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _make_customer(s)
        row = s.scalars(select(models.Customer)).one()
        assert row.tags == []
        row.tags = ["pool", "vip"]
        s.commit()
        reloaded = s.scalars(select(models.Customer)).one()
        assert reloaded.tags == ["pool", "vip"]


def test_equipment_warranty_end_column(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        pool = models.Pool(property_id=prop.id)
        s.add(pool)
        s.commit()
        s.add(models.Equipment(pool_id=pool.id))
        s.commit()
        row = s.scalars(select(models.Equipment)).one()
        assert row.warranty_end is None
        row.warranty_end = date(2027, 1, 1)
        s.commit()
        reloaded = s.scalars(select(models.Equipment)).one()
        assert reloaded.warranty_end == date(2027, 1, 1)


def test_photo_tag_column_default(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.Photo(entity_type="pool", entity_id=1, filename="a.jpg",
                           thumb_filename="a_t.jpg"))
        s.commit()
        row = s.scalars(select(models.Photo)).one()
        assert row.tag == ""


def test_job_rule_id_column(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        rule = models.MaintenanceRule(property_id=prop.id, title="Filter clean",
                                      next_due=date(2026, 8, 1))
        s.add(rule)
        s.commit()
        job = models.Job(property_id=prop.id, title="Clean filter", rule_id=rule.id)
        s.add(job)
        s.commit()
        row = s.scalars(select(models.Job)).one()
        assert row.rule_id == rule.id


def test_job_rule_id_nullable(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = _make_customer(s)
        prop = _make_property(s, cust.id)
        s.add(models.Job(property_id=prop.id, title="Ad hoc"))
        s.commit()
        row = s.scalars(select(models.Job)).one()
        assert row.rule_id is None


def test_normalize_tags_golden_case_and_dedupe():
    assert models.normalize_tags(" Pool, POOL, x") == ["pool", "x"]


def test_normalize_tags_caps_at_ten():
    raw = ",".join(f"tag{i}" for i in range(12))
    result = models.normalize_tags(raw)
    assert len(result) == 10
    assert result == [f"tag{i}" for i in range(10)]


def test_normalize_tags_drops_long_tag():
    long_tag = "a" * 30
    assert models.normalize_tags(f"ok,{long_tag}") == ["ok"]


def test_normalize_tags_drops_empties_and_strips():
    assert models.normalize_tags(" , , pool , ,algae ") == ["pool", "algae"]


def test_normalize_tags_empty_string():
    assert models.normalize_tags("") == []

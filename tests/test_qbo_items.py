"""Billing items resolve themselves, at the right granularity, exactly once.

Two guarantees are load-bearing here:

* An item is keyed on the KIND of work (a template, a chemical, a job type),
  never on a free-text label. OpenPoolPro cannot delete a QuickBooks record, so
  an item created from an edited job title would be permanent clutter.
* Resolution is adopt-before-create and recorded immediately, so billing the
  same kind twice never produces a second item.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, qbo_api, qbo_sync

SCOPE = "sandbox:9130000"


def _connected(engine):
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        for key, value in {
            "qbo_client_id": "client-abc", "qbo_client_secret": "app-secret",
            "qbo_environment": "sandbox", "qbo_refresh_token": "rt-1",
            "qbo_realm_id": "9130000", "qbo_income_account_id": "88",
        }.items():
            models.set_setting(s, key, value)
        s.commit()


# ---------- keys describe the kind of work, not its label ----------

def test_a_templated_job_keys_on_its_template(engine):
    _connected(engine)
    with Session(engine) as s:
        tmpl = models.JobTemplate(title="Green-to-clean recovery", job_type="service")
        s.add(tmpl)
        s.flush()
        job = models.Job(property_id=1, title="Green to clean - Rivera pool",
                         job_type="service", job_template_id=tmpl.id)
        job.job_template = tmpl
        key, name = qbo_sync.item_key_for_job(job)
    # The edited title is ignored; the template is what the work IS.
    assert (key, name) == (f"template:{tmpl.id}", "Green-to-clean recovery")


def test_an_ad_hoc_repair_falls_back_to_its_job_type(engine):
    _connected(engine)
    job = models.Job(property_id=1, title="Replace pump seal", job_type="repair")
    job.job_template_id = None
    assert qbo_sync.item_key_for_job(job) == ("jobtype:repair", "Pool Service — Repair")


def test_an_ad_hoc_service_job_shares_the_labor_item(engine):
    job = models.Job(property_id=1, title="Anything at all", job_type="service")
    job.job_template_id = None
    assert qbo_sync.item_key_for_job(job) == qbo_sync.item_key_for_visit()


def test_a_template_with_a_blank_title_falls_back(engine):
    """A template is only a better answer than the job type when it says something."""
    tmpl = models.JobTemplate(title="   ", job_type="repair")
    job = models.Job(property_id=1, title="x", job_type="repair")
    job.job_template_id, job.job_template = 7, tmpl
    assert qbo_sync.item_key_for_job(job)[0] == "jobtype:repair"


def test_a_chemical_keys_on_its_id_not_its_name(engine):
    """Renaming a chemical must not strand the mapping and mint a second item."""
    chem = models.Chemical(id=4, name="Liquid chlorine", unit="gal")
    assert qbo_sync.item_key_for_chemical(chem) == ("chemical:4", "Liquid chlorine")
    chem.name = "Liquid chlorine 12.5%"
    assert qbo_sync.item_key_for_chemical(chem)[0] == "chemical:4"


# ---------- resolution ----------

def test_resolve_creates_and_records_the_item(engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: None)
    made = []
    monkeypatch.setattr(qbo_api, "create_service_item", lambda s, name, account: made.append(
        (name, account)) or {"id": "77", "name": name, "type": "Service", "active": True})
    with Session(engine) as s:
        assert qbo_sync.resolve_item(s, "chemical:4", "Liquid chlorine") == "77"
    assert made == [("Liquid chlorine", "88")]
    with Session(engine) as s:
        row = s.scalar(select(models.QBOItemMap).where(models.QBOItemMap.item_key == "chemical:4"))
        assert (row.qbo_item_id, row.scope, row.name) == ("77", SCOPE, "Liquid chlorine")


def test_resolving_the_same_kind_twice_never_makes_a_second_item(engine, monkeypatch):
    _connected(engine)
    calls = []
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: calls.append(name) or None)
    monkeypatch.setattr(qbo_api, "create_service_item", lambda s, name, account: calls.append(
        "CREATE") or {"id": "77", "name": name, "type": "Service", "active": True})
    with Session(engine) as s:
        qbo_sync.resolve_item(s, "chemical:4", "Liquid chlorine")
    with Session(engine) as s:
        qbo_sync.resolve_item(s, "chemical:4", "Liquid chlorine")
    assert calls.count("CREATE") == 1
    assert len(calls) == 2, "the second resolve should not have queried QuickBooks at all"


def test_resolve_adopts_an_existing_item_of_the_same_name(engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: {
        "id": "55", "name": name, "type": "Service", "active": True})
    monkeypatch.setattr(qbo_api, "create_service_item",
                        lambda *a, **k: pytest.fail("must adopt, not create"))
    with Session(engine) as s:
        assert qbo_sync.resolve_item(s, "template:2", "Green-to-clean recovery") == "55"


def test_resolve_refuses_an_unsafe_existing_item(engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: {
        "id": "55", "name": name, "type": "Inventory", "active": True})
    with Session(engine) as s:
        with pytest.raises(qbo_sync.SyncError, match="Inventory"):
            qbo_sync.resolve_item(s, "template:2", "Chlorine tabs")
        assert s.scalar(select(models.QBOItemMap)) is None


def test_resolve_refuses_an_inactive_existing_item(engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: {
        "id": "55", "name": name, "type": "Service", "active": False})
    with Session(engine) as s:
        with pytest.raises(qbo_sync.SyncError, match="inactive"):
            qbo_sync.resolve_item(s, "template:2", "Retired service")


def test_resolve_without_an_income_account_says_what_to_do(engine, monkeypatch):
    _connected(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_income_account_id", "")
        s.commit()
    monkeypatch.setattr(qbo_api, "find_item_by_name", lambda s, name: None)
    with Session(engine) as s:
        with pytest.raises(qbo_sync.SyncError, match="income account"):
            qbo_sync.resolve_item(s, "service", "Pool Service — Labor")


def test_missing_items_reports_each_kind_once_and_writes_nothing(engine, monkeypatch):
    _connected(engine)
    monkeypatch.setattr(qbo_api, "find_item_by_name",
                        lambda *a, **k: pytest.fail("missing_items must not call QuickBooks"))
    lines = [
        {"item_key": "service", "item_name": "Pool Service — Labor"},
        {"item_key": "service", "item_name": "Pool Service — Labor"},
        {"item_key": "chemical:4", "item_name": "Liquid chlorine"},
        {"item_kind": "service"},  # a legacy line, no key
    ]
    with Session(engine) as s:
        assert qbo_sync.missing_items(s, lines) == [
            ("service", "Pool Service — Labor"), ("chemical:4", "Liquid chlorine")]
        assert s.scalar(select(models.QBOItemMap)) is None


def test_an_item_map_is_scoped_to_its_company(engine, monkeypatch):
    """A sandbox item id must never be billed against a production company."""
    _connected(engine)
    with Session(engine) as s:
        s.add(models.QBOItemMap(scope="production:999", item_key="service", qbo_item_id="55"))
        s.commit()
        assert qbo_sync.mapped_item(s, "service") == ""

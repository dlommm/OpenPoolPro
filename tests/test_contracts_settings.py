from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _post_contract_rates(client, **overrides):
    data = {
        "contract_rate_id": [""],
        "max_gallons": ["15000"],
        "label": ["Small"],
        "weekly_dollars": ["48.00"],
        "open_close_dollars": ["120.00"],
        "green_clean_dollars": ["200.00"],
        "storm_dollars": ["75.00"],
        "service_call_dollars": ["90.00"],
        "spa_dollars": ["25.00"],
    }
    data.update(overrides)
    return client.post("/settings/contract_rates", data=data, follow_redirects=False)


def test_add_contract_rate_persists_cents(client, engine):
    r = _post_contract_rates(client)
    assert r.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.ContractRate)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.label == "Small"
        assert row.max_gallons == 15000
        assert row.weekly_cents == 4800
        assert row.open_close_cents == 12000
        assert row.green_clean_cents == 20000
        assert row.storm_cents == 7500
        assert row.service_call_cents == 9000
        assert row.spa_cents == 2500


def test_edit_existing_contract_rate(client, engine):
    with Session(engine) as s:
        row = models.ContractRate(max_gallons=15000, label="Small", weekly_cents=1000,
                                  open_close_cents=2000, green_clean_cents=3000,
                                  storm_cents=4000, service_call_cents=5000, spa_cents=6000)
        s.add(row)
        s.commit()
        rid = row.id

    _post_contract_rates(client, **{"contract_rate_id": [str(rid)], "max_gallons": ["20000"],
                                    "label": ["Medium"], "weekly_dollars": ["55.00"]})
    with Session(engine) as s:
        rows = s.scalars(select(models.ContractRate)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == rid
        assert row.label == "Medium"
        assert row.max_gallons == 20000
        assert row.weekly_cents == 5500
        # untouched-in-override fields still round-trip from the posted (default) values
        assert row.open_close_cents == 12000


def test_blank_weekly_dollars_deletes_row(client, engine):
    with Session(engine) as s:
        row = models.ContractRate(max_gallons=15000, label="Small", weekly_cents=1000)
        s.add(row)
        s.commit()
        rid = row.id
        before = len(s.scalars(select(models.ContractRate)).all())

    client.post("/settings/contract_rates", data={
        "contract_rate_id": [str(rid)], "max_gallons": [""], "label": [""],
        "weekly_dollars": [""], "open_close_dollars": [""], "green_clean_dollars": [""],
        "storm_dollars": [""], "service_call_dollars": [""], "spa_dollars": [""]})
    with Session(engine) as s:
        assert len(s.scalars(select(models.ContractRate)).all()) == before - 1
        assert s.get(models.ContractRate, rid) is None


def test_blank_optional_dollar_fields_default_to_zero(client, engine):
    _post_contract_rates(client, open_close_dollars=[""], spa_dollars=[""])
    with Session(engine) as s:
        row = s.scalars(select(models.ContractRate)).one()
        assert row.open_close_cents == 0
        assert row.spa_cents == 0
        assert row.weekly_cents == 4800  # untouched field still saved


def test_garbage_row_is_tolerant_no_500(client, engine):
    r = _post_contract_rates(client, max_gallons=["not-a-number"])
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.scalars(select(models.ContractRate)).all() == []

    r = _post_contract_rates(client, weekly_dollars=["fifty bucks"])
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.scalars(select(models.ContractRate)).all() == []


def test_stale_and_garbage_contract_rate_ids_skipped(client, engine):
    r = client.post("/settings/contract_rates", data={
        "contract_rate_id": ["9999", "abc"], "max_gallons": ["", ""],
        "label": ["Stale", "Garbage"], "weekly_dollars": ["50", "60"],
        "open_close_dollars": ["", ""], "green_clean_dollars": ["", ""],
        "storm_dollars": ["", ""], "service_call_dollars": ["", ""], "spa_dollars": ["", ""]},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.scalars(select(models.ContractRate)).all() == []


def test_contract_rate_edit_writes_rates_change_audit_row(client, engine):
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    _post_contract_rates(client)
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.change")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id


def test_settings_page_renders_existing_contract_rates(client, engine):
    with Session(engine) as s:
        s.add(models.ContractRate(max_gallons=15000, label="Small", weekly_cents=4800,
                                  open_close_cents=12000, green_clean_cents=20000,
                                  storm_cents=7500, service_call_cents=9000, spa_cents=2500))
        s.commit()

    r = client.get("/settings")
    assert r.status_code == 200
    assert "Contract rates by pool size" in r.text
    assert "Small" in r.text
    assert "48.00" in r.text
    assert "120.00" in r.text
    assert "25.00" in r.text

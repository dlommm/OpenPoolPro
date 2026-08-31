"""The /contracts hub: register, chips, needs-attention digest, pool picker,
duplicate guard, season renewal, and resend. Workflow transitions themselves
are covered in test_contracts_routes.py."""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

YEAR = date.today().year


def _seed_property(session: Session, name="Riviera Household", address="1 Cove Rd"):
    customer = models.Customer(name=name, email="riviera@example.com")
    prop = models.Property(customer=customer, label="Home", address=address)
    session.add(prop)
    session.commit()
    pool = models.Pool(property_id=prop.id, label="Main pool", kind="pool",
                       gallons=12000, active=True)
    session.add(pool)
    session.commit()
    return customer, prop, pool


def _seed_rate(session: Session, weekly=4800):
    session.add(models.ContractRate(
        max_gallons=15000, label="Small", weekly_cents=weekly,
        open_close_cents=12000, green_clean_cents=20000,
        storm_cents=7500, service_call_cents=9000, spa_cents=2500))
    session.commit()


def _create(client, pool_id, season=None):
    data = {"season_year": str(season)} if season else {}
    r = client.post(f"/pools/{pool_id}/contracts/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])


def test_hub_renders_with_counts_and_rows(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)

    r = client.get("/contracts")
    assert r.status_code == 200
    assert "Riviera Household" in r.text
    assert f"/contracts/{cid}" in r.text
    assert "Draft 1" in r.text


def test_hub_status_filter_and_search(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool_a = _seed_property(s, name="Alvarez", address="3 Bay Rd")
        _, _, pool_b = _seed_property(s, name="Henderson", address="12 Oak Ln")
        ids = (pool_a.id, pool_b.id)
    _create(client, ids[0])
    cid_b = _create(client, ids[1])
    client.post(f"/contracts/{cid_b}/approve")

    r = client.get("/contracts", params={"status": "approved"})
    assert "Henderson" in r.text and "Alvarez" not in r.text

    r = client.get("/contracts", params={"q": "Alvarez"})
    assert "Alvarez" in r.text and "Henderson" not in r.text

    # search matches addresses too
    r = client.get("/contracts", params={"q": "Oak"})
    assert "Henderson" in r.text and "Alvarez" not in r.text


def test_hub_hides_money_without_view_money(engine, client, tech_client):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    _create(client, pool_id)

    tc = tech_client(name="mona", manage_customers=True)
    r = tc.get("/contracts")
    assert r.status_code == 200
    assert "Signed value" not in r.text and "Season value" not in r.text

    r = client.get("/contracts")  # owner/admin sees totals
    assert "Signed value" in r.text


def test_hub_needs_attention_stale_sent_and_uncovered(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s, name="Henderson")
        _seed_property(s, name="Alvarez", address="3 Bay Rd")  # pool, no contract
        pool_id = pool.id
    cid = _create(client, pool_id)
    with Session(engine) as s:
        c = s.get(models.Contract, cid)
        c.status = "sent"
        c.sent_at = models.utcnow() - timedelta(days=20)
        s.commit()

    r = client.get("/contracts")
    assert "Needs attention" in r.text
    assert "sent 20 days ago" in r.text
    assert f"no {YEAR} contract" in r.text
    assert "Alvarez" in r.text


def test_duplicate_guard_redirects_to_existing(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    cid2 = _create(client, pool_id)  # same pool, same season
    assert cid2 == cid
    with Session(engine) as s:
        assert len(s.scalars(select(models.Contract)).all()) == 1


def test_duplicate_guard_ignores_void_and_other_seasons(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    other = _create(client, pool_id, season=YEAR + 1)  # different season: new row
    assert other != cid
    client.post(f"/contracts/{cid}/void")
    replacement = _create(client, pool_id)  # void doesn't block a redo
    assert replacement not in (cid, other)


def test_picker_page_lists_pools_and_existing_contract(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id

    r = client.get("/contracts/new")
    assert r.status_code == 200
    assert "Riviera Household" in r.text and "Create draft" in r.text

    cid = _create(client, pool_id)
    r = client.get("/contracts/new")
    assert f"/contracts/{cid}" in r.text  # existing contract linked, not re-creatable
    assert "Create draft" not in r.text


def test_renew_preview_and_create_reprices_from_current_rates(client, engine):
    with Session(engine) as s:
        _seed_rate(s, weekly=4800)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    with Session(engine) as s:
        s.get(models.Contract, cid).status = "signed"
        rate = s.scalars(select(models.ContractRate)).one()
        rate.weekly_cents = 5300  # price bump since last season's snapshot
        s.commit()

    r = client.get("/contracts/renew", params={"from_season": YEAR})
    assert r.status_code == 200
    assert "Riviera Household" in r.text and "Create 1 draft" in r.text

    r = client.post("/contracts/renew",
                    data={"from_season": str(YEAR), "to_season": str(YEAR + 1)},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        renewed = s.scalars(select(models.Contract).where(
            models.Contract.season_year == YEAR + 1)).one()
        assert renewed.status == "draft"
        assert renewed.pool_id == pool_id
        assert renewed.weekly_cents == 5300
        audit = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.renew")).one()
        assert "1 drafts" in audit.detail


def test_renew_skips_pools_already_renewed_and_rejects_same_season(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    with Session(engine) as s:
        s.get(models.Contract, cid).status = "signed"
        s.commit()
    _create(client, pool_id, season=YEAR + 1)  # already renewed by hand

    r = client.get("/contracts/renew", params={"from_season": YEAR})
    assert "Nothing to renew" in r.text

    r = client.post("/contracts/renew",
                    data={"from_season": str(YEAR), "to_season": str(YEAR)},
                    follow_redirects=False)
    assert r.status_code == 400


def test_resend_allowed_from_sent(client, engine, monkeypatch):
    sent = []
    monkeypatch.setattr("app.mailer.smtp_configured", lambda session: True)
    monkeypatch.setattr("app.mailer.smtp_settings", lambda session: object())
    monkeypatch.setattr("app.mailer.send_message",
                        lambda *a, **kw: sent.append(a))
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    client.post(f"/contracts/{cid}/approve")
    assert client.post(f"/contracts/{cid}/send", follow_redirects=False).status_code == 303
    assert client.post(f"/contracts/{cid}/send", follow_redirects=False).status_code == 303
    assert len(sent) == 2
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "sent"


def test_detail_timeline_lists_lifecycle_events(client, engine):
    with Session(engine) as s:
        _seed_rate(s)
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _create(client, pool_id)
    client.post(f"/contracts/{cid}/approve")
    client.post(f"/contracts/{cid}/void")

    r = client.get(f"/contracts/{cid}")
    assert r.status_code == 200
    for label in ("History", "Created", "Approved", "Voided"):
        assert label in r.text


def test_hub_403_without_manage_customers(client, engine, tech_client):
    tc = tech_client()
    for path in ("/contracts", "/contracts/new", "/contracts/renew"):
        assert tc.get(path).status_code == 403
    assert tc.post("/contracts/renew", data={}).status_code == 403

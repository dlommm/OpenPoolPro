import os
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models


def _seed_property(session: Session, with_spa: bool = False):
    customer = models.Customer(name="Riviera Household", email="riviera@example.com")
    prop = models.Property(customer=customer, label="Home", address="1 Cove Rd")
    session.add(prop)
    session.commit()
    pool = models.Pool(property_id=prop.id, label="Main pool", kind="pool",
                       gallons=12000, active=True)
    session.add(pool)
    if with_spa:
        spa = models.Pool(property_id=prop.id, label="Spa", kind="spa",
                          gallons=500, active=True)
        session.add(spa)
    session.commit()
    return customer, prop, pool


def _seed_contract_rate(session: Session, max_gallons=15000):
    row = models.ContractRate(
        max_gallons=max_gallons, label="Small",
        weekly_cents=4800, open_close_cents=12000, green_clean_cents=20000,
        storm_cents=7500, service_call_cents=9000, spa_cents=2500)
    session.add(row)
    session.commit()


def _new_contract(client, pool_id):
    return client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)


def _contract_id_from_redirect(r) -> int:
    assert r.status_code == 303
    location = r.headers["location"]
    return int(location.rstrip("/").rsplit("/", 1)[-1])


def test_new_from_pool_snapshots_rates_and_includes_spa(client, engine):
    with Session(engine) as s:
        _seed_contract_rate(s)
        _, _, pool = _seed_property(s, with_spa=True)
        pool_id = pool.id

    r = _new_contract(client, pool_id)
    cid = _contract_id_from_redirect(r)

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract is not None
        assert contract.status == "draft"
        assert contract.season_year == date.today().year
        assert contract.weeks == 16
        assert contract.includes_spa is True
        assert contract.weekly_cents == 4800
        assert contract.open_close_cents == 12000
        assert contract.green_clean_cents == 20000
        assert contract.storm_cents == 7500
        assert contract.service_call_cents == 9000
        assert contract.spa_cents == 2500


def test_new_from_pool_no_spa_on_property_is_false(client, engine):
    with Session(engine) as s:
        _seed_contract_rate(s)
        _, _, pool = _seed_property(s, with_spa=False)
        pool_id = pool.id

    r = _new_contract(client, pool_id)
    cid = _contract_id_from_redirect(r)
    with Session(engine) as s:
        assert s.get(models.Contract, cid).includes_spa is False


def test_new_from_pool_no_rate_match_defaults_to_zero(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)  # no ContractRate rows seeded at all
        pool_id = pool.id

    r = _new_contract(client, pool_id)
    cid = _contract_id_from_redirect(r)
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.weekly_cents == 0
        assert contract.spa_cents == 0


def test_edit_updates_draft(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.post(f"/contracts/{cid}/edit", data={
        "season_year": "2027", "weeks": "20",
        "open_date": "2027-04-01", "close_date": "2027-10-15",
        "weekly_dollars": "55.50", "open_close_dollars": "130.00",
        "green_clean_dollars": "210.00", "storm_dollars": "80.00",
        "service_call_dollars": "95.00", "spa_dollars": "30.00",
    }, follow_redirects=False)
    assert r.status_code == 303

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.season_year == 2027
        assert contract.weeks == 20
        assert contract.open_date == date(2027, 4, 1)
        assert contract.close_date == date(2027, 10, 15)
        assert contract.weekly_cents == 5550
        assert contract.open_close_cents == 13000
        assert contract.green_clean_cents == 21000
        assert contract.storm_cents == 8000
        assert contract.service_call_cents == 9500
        assert contract.spa_cents == 3000


def test_edit_rejected_once_not_draft(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)

    r = client.post(f"/contracts/{cid}/edit", data={"weekly_dollars": "999.00"},
                    follow_redirects=False)
    assert r.status_code == 400


def test_approve_writes_pdf_flips_status_and_audits(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.post(f"/contracts/{cid}/approve", follow_redirects=False)
    assert r.status_code == 303

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "approved"
        assert contract.pdf_filename == "contract.pdf"
        assert contract.approved_at is not None
        assert contract.approved_by_id is not None
        path = os.path.join(config.DATA_DIR, "contracts", str(cid), "contract.pdf")
        assert os.path.isfile(path)
        assert open(path, "rb").read().startswith(b"%PDF")

        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any(row.detail == f"{cid} -> approved" for row in rows)


def test_send_emails_pdf_attachment_and_flips_to_sent(client, engine, monkeypatch):
    from app import mailer

    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)

    captured = {}

    def fake_send_message(to_addr, subject, body, conf=None, attachments=None, html=None):
        captured["to_addr"] = to_addr
        captured["subject"] = subject
        captured["attachments"] = attachments

    monkeypatch.setattr(mailer, "send_message", fake_send_message)

    r = client.post(f"/contracts/{cid}/send", follow_redirects=False)
    assert r.status_code == 303

    assert captured["to_addr"] == "riviera@example.com"
    assert captured["attachments"] is not None
    assert len(captured["attachments"]) == 1
    pdf_bytes, filename = captured["attachments"][0]
    assert filename == "contract.pdf"
    assert pdf_bytes.startswith(b"%PDF")

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "sent"
        assert contract.sent_at is not None
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any(row.detail.startswith(f"{cid} -> sent") for row in rows)


def test_send_without_email_or_smtp_stays_approved(client, engine):
    with Session(engine) as s:
        customer = models.Customer(name="No Email Co")  # blank email
        prop = models.Property(customer=customer, label="Home")
        s.add(prop)
        s.commit()
        pool = models.Pool(property_id=prop.id, label="Main", kind="pool", gallons=5000)
        s.add(pool)
        s.commit()
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)

    r = client.post(f"/contracts/{cid}/send", follow_redirects=False)
    assert r.status_code == 303
    assert "send_error=1" in r.headers["location"]

    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "approved"


def test_send_requires_approved_status(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))  # still draft

    r = client.post(f"/contracts/{cid}/send", follow_redirects=False)
    assert r.status_code == 400


def test_mark_signed_stores_uploaded_pdf_and_flips_to_signed(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.post(f"/contracts/{cid}/mark-signed",
                    files={"file": ("signed.pdf", b"%PDF-1.4 fake signed bytes",
                                    "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "signed"
        assert contract.signed_pdf_filename == "signed.pdf"
        assert contract.signed_at is not None
        path = os.path.join(config.DATA_DIR, "contracts", str(cid), "signed.pdf")
        assert os.path.isfile(path)
        assert open(path, "rb").read() == b"%PDF-1.4 fake signed bytes"
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any(row.detail == f"{cid} -> signed" for row in rows)


def test_mark_signed_rejects_non_pdf_bytes(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.post(f"/contracts/{cid}/mark-signed",
                    files={"file": ("signed.pdf", b"not a pdf", "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "sign_error=1" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "draft"


def test_void(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.post(f"/contracts/{cid}/void", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "void"
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any(row.detail == f"{cid} -> void" for row in rows)

    # void is terminal
    r = client.post(f"/contracts/{cid}/void", follow_redirects=False)
    assert r.status_code == 400


def test_pdf_serve_404_before_approve_200_after(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    r = client.get(f"/contracts/{cid}/pdf")
    assert r.status_code == 404

    client.post(f"/contracts/{cid}/approve", follow_redirects=False)
    r = client.get(f"/contracts/{cid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


def test_signed_serve_404_before_upload(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))
    r = client.get(f"/contracts/{cid}/signed")
    assert r.status_code == 404


def test_detail_page_renders(client, engine):
    with Session(engine) as s:
        _seed_contract_rate(s)
        _, _, pool = _seed_property(s, with_spa=True)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))
    r = client.get(f"/contracts/{cid}")
    assert r.status_code == 200
    assert "Riviera Household" in r.text


def test_tech_without_manage_customers_403_sweep(client, engine, tech_client):
    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
    cid = _contract_id_from_redirect(_new_contract(client, pool_id))

    t = tech_client(name="notadmin")
    assert t.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False).status_code == 403
    assert t.get(f"/contracts/{cid}", follow_redirects=False).status_code == 403
    assert t.post(f"/contracts/{cid}/edit", follow_redirects=False).status_code == 403
    assert t.post(f"/contracts/{cid}/approve", follow_redirects=False).status_code == 403
    assert t.post(f"/contracts/{cid}/send", follow_redirects=False).status_code == 403
    assert t.post(f"/contracts/{cid}/mark-signed",
                  files={"file": ("s.pdf", b"%PDF fake", "application/pdf")},
                  follow_redirects=False).status_code == 403
    assert t.post(f"/contracts/{cid}/void", follow_redirects=False).status_code == 403
    assert t.get(f"/contracts/{cid}/pdf", follow_redirects=False).status_code == 403
    assert t.get(f"/contracts/{cid}/signed", follow_redirects=False).status_code == 403

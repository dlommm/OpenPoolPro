"""Public contract e-sign flow: token minting on send, the /sign pages, and
the signing transaction itself."""
import os
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models


def _seed_property(session: Session):
    customer = models.Customer(name="Riviera Household", email="riviera@example.com")
    prop = models.Property(customer=customer, label="Home", address="1 Cove Rd")
    session.add(prop)
    session.commit()
    pool = models.Pool(property_id=prop.id, label="Main pool", kind="pool",
                       gallons=12000, active=True)
    session.add(pool)
    session.commit()
    return customer, prop, pool


def _sent_contract(client, engine, monkeypatch, link_expiry="168"):
    """Draft -> approve -> send with a captured email; returns (cid, token, body)."""
    from app import mailer

    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()

    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)

    captured = {}

    def fake_send_message(to_addr, subject, body, conf=None, attachments=None):
        captured["body"] = body

    monkeypatch.setattr(mailer, "send_message", fake_send_message)
    r = client.post(f"/contracts/{cid}/send", data={"link_expiry": link_expiry},
                    follow_redirects=False)
    assert r.status_code == 303

    m = re.search(r"/sign/([A-Za-z0-9_-]+)", captured["body"])
    assert m, "sign link missing from the email body"
    return cid, m.group(1), captured["body"]


def test_send_mints_hashed_token_with_chosen_expiry(client, engine, monkeypatch):
    cid, token, body = _sent_contract(client, engine, monkeypatch, link_expiry="72")
    assert "3 days" in body
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.sign_token_hash
        assert contract.sign_token_hash != token  # only the hash is stored
        assert token not in contract.sign_token_hash
        remaining = contract.sign_token_expires_at - models.utcnow()
        assert timedelta(hours=71) < remaining <= timedelta(hours=72)


def test_sign_page_renders_without_a_session(client, engine, monkeypatch):
    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()  # the customer has no login
    r = client.get(f"/sign/{token}")
    assert r.status_code == 200
    assert "Riviera Household" in r.text
    assert "terms and conditions" in r.text
    r = client.get(f"/sign/{token}/pdf")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_invalid_token_is_rejected(client, engine, monkeypatch):
    _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    r = client.get("/sign/not-a-real-token")
    assert r.status_code == 404
    assert "not valid" in r.text


def test_expired_token_is_rejected(client, engine, monkeypatch):
    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        contract.sign_token_expires_at = models.utcnow() - timedelta(hours=1)
        s.commit()
    client.cookies.clear()
    r = client.get(f"/sign/{token}")
    assert r.status_code == 410
    assert "expired" in r.text


def test_resend_invalidates_previous_link(client, engine, monkeypatch):
    from app import mailer

    cid, old_token, _ = _sent_contract(client, engine, monkeypatch)
    captured = {}
    monkeypatch.setattr(mailer, "send_message",
                        lambda to, sub, body, conf=None, attachments=None:
                        captured.__setitem__("body", body))
    client.post(f"/contracts/{cid}/send", data={"link_expiry": "24"},
                follow_redirects=False)
    new_token = re.search(r"/sign/([A-Za-z0-9_-]+)", captured["body"]).group(1)
    assert new_token != old_token

    client.cookies.clear()
    assert client.get(f"/sign/{old_token}").status_code == 404
    assert client.get(f"/sign/{new_token}").status_code == 200


def test_submit_requires_name_and_agreement(client, engine, monkeypatch):
    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    r = client.post(f"/sign/{token}", data={"signed_name": "", "agree": ""})
    assert r.status_code == 422
    r = client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": ""})
    assert r.status_code == 422
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "sent"


def test_submit_signs_and_registers_the_contract(client, engine, monkeypatch):
    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()

    r = client.post(f"/sign/{token}", data={"signed_name": "Ada  Rivera",
                                            "agree": "yes"})
    assert r.status_code == 200
    assert "Thank you" in r.text

    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "signed"
        assert contract.signed_at is not None
        assert contract.signed_name == "Ada Rivera"  # whitespace collapsed
        assert contract.signed_pdf_filename == "signed.pdf"
        assert contract.sign_token_hash == ""  # single-use
        assert contract.sign_token_expires_at is None

        path = os.path.join(config.DATA_DIR, "contracts", str(cid), "signed.pdf")
        assert os.path.isfile(path)
        signed = open(path, "rb").read()
        assert signed.startswith(b"%PDF")
        original = open(os.path.join(config.DATA_DIR, "contracts", str(cid),
                                     "contract.pdf"), "rb").read()
        assert len(signed) > len(original)  # certificate page appended

        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any("signed (e-sign by Ada Rivera)" in row.detail for row in rows)

    # The spent link (and its PDF route) is dead afterwards.
    assert client.get(f"/sign/{token}").status_code == 404
    assert client.get(f"/sign/{token}/pdf").status_code == 404


def test_signed_certificate_page_carries_signer_details(client, engine, monkeypatch):
    from pypdf import PdfReader

    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})

    import io
    path = os.path.join(config.DATA_DIR, "contracts", str(cid), "signed.pdf")
    reader = PdfReader(io.BytesIO(open(path, "rb").read()))
    cert_text = reader.pages[-1].extract_text()
    assert "Certificate of Electronic Signature" in cert_text
    assert "Ada Rivera" in cert_text


def test_public_base_url_setting_wins_in_email_link(client, engine, monkeypatch):
    from app import mailer

    with Session(engine) as s:
        _, _, pool = _seed_property(s)
        pool_id = pool.id
        models.set_setting(s, "smtp_host", "smtp.example.com")
        models.set_setting(s, "public_base_url", "https://pool.example.com")
        s.commit()
    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)

    captured = {}
    monkeypatch.setattr(mailer, "send_message",
                        lambda to, sub, body, conf=None, attachments=None:
                        captured.__setitem__("body", body))
    client.post(f"/contracts/{cid}/send", follow_redirects=False)
    assert "https://pool.example.com/sign/" in captured["body"]


def test_invalid_token_probing_gets_rate_limited(client, engine, monkeypatch):
    _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    for _ in range(20):
        client.get("/sign/wrong-token")
    r = client.get("/sign/wrong-token")
    assert r.status_code == 429

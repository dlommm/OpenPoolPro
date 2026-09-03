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
    # DocuSign-style page: the agreement renders in-page as HTML (no iframe),
    # with the signature panel embedded at the document's signature block.
    assert "<iframe" not in r.text
    assert "Service Rates" in r.text
    assert "Sign here" in r.text
    assert "GreatVibes" in r.text  # script-font signature preview
    r = client.get(f"/sign/{token}/pdf")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    # Frameable by our own pages and displayed inline, not downloaded —
    # X-Frame-Options: DENY silently blanked every same-origin PDF preview.
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["content-disposition"].startswith("inline")


def test_signer_ip_is_the_customer_public_ip_not_the_proxy(client, engine, monkeypatch):
    """The certificate must record the customer's public IP even through a
    multi-hop proxy chain (Pangolin tunnel -> Traefik), taken from the
    leftmost X-Forwarded-For entry."""
    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    r = client.post(f"/sign/{token}",
                    data={"signed_name": "Ada Rivera", "agree": "yes"},
                    headers={"x-forwarded-for": "203.0.113.9, 172.18.0.5, 172.18.0.2"})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(models.Contract, cid).signed_ip == "203.0.113.9"


def test_signing_emails_the_signed_copy(client, engine, monkeypatch):
    from app import mailer

    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    calls = []
    monkeypatch.setattr(mailer, "send_message",
                        lambda to, sub, body, conf=None, attachments=None:
                        calls.append((to, sub, attachments)))
    client.cookies.clear()
    r = client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})
    assert r.status_code == 200
    assert "emailed to" in r.text and "riviera@example.com" in r.text
    to, subject, attachments = calls[-1]
    assert to == "riviera@example.com"
    assert "Signed" in subject
    pdf_bytes, filename = attachments[0]
    assert filename == "signed-contract.pdf"
    assert pdf_bytes.startswith(b"%PDF")


def test_signing_succeeds_even_when_copy_email_fails(client, engine, monkeypatch):
    from app import mailer

    cid, token, _ = _sent_contract(client, engine, monkeypatch)

    def boom(*a, **k):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(mailer, "send_message", boom)
    client.cookies.clear()
    r = client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})
    assert r.status_code == 200
    assert "Thank you" in r.text  # signing recorded regardless
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "signed"


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
        # Signed PDF hash is recorded (tamper-evidence) and matches the file.
        import hashlib
        assert contract.signed_pdf_hash == hashlib.sha256(signed).hexdigest()

        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "contract.status")).all()
        assert any("signed (e-sign by Ada Rivera" in row.detail for row in rows)

    # The spent link (and its PDF route) is dead afterwards.
    assert client.get(f"/sign/{token}").status_code == 404
    assert client.get(f"/sign/{token}/pdf").status_code == 404


def test_signature_is_inline_no_separate_certificate_page(client, engine, monkeypatch):
    import io

    from pypdf import PdfReader

    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    client.cookies.clear()
    client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})

    path = os.path.join(config.DATA_DIR, "contracts", str(cid), "signed.pdf")
    signed = PdfReader(io.BytesIO(open(path, "rb").read()))
    text = "".join(p.extract_text() for p in signed.pages)
    assert "Ada Rivera" in text  # signature is in the document
    # No separate certificate page, and the customer copy never shows the IP.
    assert "Certificate of Completion" not in text
    assert "Signer IP" not in text


def test_certificate_of_completion_is_staff_only_and_has_audit_details(client, engine, monkeypatch):
    import io

    from pypdf import PdfReader

    cid, token, _ = _sent_contract(client, engine, monkeypatch)
    # Customer opens the link (records viewed), then signs from a known IP.
    client.cookies.clear()
    client.get(f"/sign/{token}", headers={"x-forwarded-for": "198.51.100.9, 8.8.8.8"})
    client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"},
                headers={"x-forwarded-for": "198.51.100.9, 8.8.8.8"})

    # Staff-only route (requires the authed client fixture, re-login).
    client.post("/login", data={"username": "owner", "password": "testpass"})
    r = client.get(f"/contracts/{cid}/certificate")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    cert = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "Certificate of Completion" in cert
    assert "Ada Rivera" in cert
    assert "198.51.100.9" in cert  # signer IP lives here, not on the customer copy
    assert "ESIGN" in cert or "UETA" in cert
    with Session(engine) as s:
        assert s.get(models.Contract, cid).sign_viewed_at is not None


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

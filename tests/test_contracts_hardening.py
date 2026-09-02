"""Deep-dive hardening coverage: the error paths, race guards, and retention
behaviors the 2026-09 audit found untested."""
import io
import os
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models
from app.fileio import atomic_write
from app.maintenance import prune_history
from app.observability import redact_path


def _seed_pool(session: Session, active: bool = True):
    customer = models.Customer(name="Riviera Household", email="riviera@example.com",
                               active=active)
    prop = models.Property(customer=customer, label="Home", address="1 Cove Rd")
    session.add(prop)
    session.commit()
    pool = models.Pool(property_id=prop.id, label="Main pool", kind="pool",
                       gallons=12000, active=True)
    session.add(pool)
    session.commit()
    return customer, prop, pool


def _approved_contract(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_pool(s)
        pool_id = pool.id
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()
    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)
    return cid


def _sent_with_token(client, engine, monkeypatch):
    from app import mailer

    cid = _approved_contract(client, engine)
    captured = {}
    monkeypatch.setattr(mailer, "send_message",
                        lambda to, sub, body, conf=None, attachments=None:
                        captured.__setitem__("body", body))
    client.post(f"/contracts/{cid}/send", follow_redirects=False)
    token = re.search(r"/sign/([A-Za-z0-9_-]+)", captured["body"]).group(1)
    return cid, token


# ---------- send failure ----------

def test_send_failure_rolls_back_token_and_status(client, engine, monkeypatch):
    from app import mailer

    cid = _approved_contract(client, engine)

    def boom(*a, **k):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(mailer, "send_message", boom)
    r = client.post(f"/contracts/{cid}/send", follow_redirects=False)
    assert r.status_code == 303 and "send_error=1" in r.headers["location"]
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "approved"  # never advanced
        assert contract.sign_token_hash == ""  # minted token discarded
        assert contract.sent_at is None


def test_invalid_link_expiry_falls_back_to_default(client, engine, monkeypatch):
    from app import mailer

    cid = _approved_contract(client, engine)
    monkeypatch.setattr(mailer, "send_message", lambda *a, **k: None)
    client.post(f"/contracts/{cid}/send", data={"link_expiry": "999999"},
                follow_redirects=False)
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        remaining = contract.sign_token_expires_at - models.utcnow()
        assert timedelta(hours=167) < remaining <= timedelta(hours=168)


# ---------- sign-time failures ----------

def test_sign_pdf_render_failure_leaves_contract_sent(client, engine, monkeypatch):
    from app import contract_pdf

    cid, token = _sent_with_token(client, engine, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("merge failed")

    real_signed_pdf = contract_pdf.signed_contract_pdf
    monkeypatch.setattr(contract_pdf, "signed_contract_pdf", boom)
    client.cookies.clear()
    r = client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})
    assert r.status_code == 500
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        assert contract.status == "sent"  # claim rolled back with the session
        assert contract.sign_token_hash != ""  # link still usable for a retry
    assert not os.path.isfile(
        os.path.join(config.DATA_DIR, "contracts", str(cid), "signed.pdf"))
    # And the retry actually works.
    monkeypatch.setattr(contract_pdf, "signed_contract_pdf", real_signed_pdf)
    r = client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})
    assert r.status_code == 200


def test_voided_contract_link_is_dead_even_before_expiry(client, engine, monkeypatch):
    cid, token = _sent_with_token(client, engine, monkeypatch)
    client.post(f"/contracts/{cid}/void", follow_redirects=False)
    client.cookies.clear()
    assert client.get(f"/sign/{token}").status_code == 410
    assert client.post(f"/sign/{token}", data={"signed_name": "A", "agree": "yes"}).status_code == 410
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "void"


def test_post_to_expired_token_rejected(client, engine, monkeypatch):
    cid, token = _sent_with_token(client, engine, monkeypatch)
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        contract.sign_token_expires_at = models.utcnow() - timedelta(hours=1)
        s.commit()
    client.cookies.clear()
    r = client.post(f"/sign/{token}", data={"signed_name": "A", "agree": "yes"})
    assert r.status_code == 410


def test_sign_pdf_missing_on_disk_is_friendly_404(client, engine, monkeypatch):
    cid, token = _sent_with_token(client, engine, monkeypatch)
    os.unlink(os.path.join(config.DATA_DIR, "contracts", str(cid), "contract.pdf"))
    client.cookies.clear()
    assert client.get(f"/sign/{token}/pdf").status_code == 404
    r = client.post(f"/sign/{token}", data={"signed_name": "A", "agree": "yes"})
    assert r.status_code == 404
    with Session(engine) as s:
        assert s.get(models.Contract, cid).status == "sent"


# ---------- lifecycle guards ----------

def test_double_approve_is_rejected(client, engine):
    cid = _approved_contract(client, engine)
    r = client.post(f"/contracts/{cid}/approve", follow_redirects=False)
    assert r.status_code == 400


def test_mark_signed_rejected_on_void(client, engine):
    cid = _approved_contract(client, engine)
    client.post(f"/contracts/{cid}/void", follow_redirects=False)
    r = client.post(f"/contracts/{cid}/mark-signed",
                    files={"file": ("s.pdf", b"%PDF-1.4 fake", "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 400


def test_signed_pdf_serves_inline_after_esign(client, engine, monkeypatch):
    cid, token = _sent_with_token(client, engine, monkeypatch)
    client.cookies.clear()
    client.post(f"/sign/{token}", data={"signed_name": "Ada Rivera", "agree": "yes"})
    # Staff view of the signed copy (re-login via fresh client fixture state).
    client.post("/login", data={"username": "owner", "password": "testpass"})
    r = client.get(f"/contracts/{cid}/signed")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"].startswith("inline")


def test_edit_cannot_create_duplicate_season(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_pool(s)
        pool_id = pool.id
    r = client.post(f"/pools/{pool_id}/contracts/new", data={"season_year": "2026"},
                    follow_redirects=False)
    cid_a = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    r = client.post(f"/pools/{pool_id}/contracts/new", data={"season_year": "2027"},
                    follow_redirects=False)
    cid_b = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    assert cid_a != cid_b

    client.post(f"/contracts/{cid_b}/edit", data={"season_year": "2026"},
                follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Contract, cid_b).season_year == 2027  # change refused


def test_edit_inf_rate_does_not_500(client, engine):
    with Session(engine) as s:
        _, _, pool = _seed_pool(s)
        pool_id = pool.id
    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    r = client.post(f"/contracts/{cid}/edit", data={"weekly_dollars": "inf"},
                    follow_redirects=False)
    assert r.status_code == 303  # tolerated, not a 500
    with Session(engine) as s:
        assert s.get(models.Contract, cid).weekly_cents == 0  # value untouched


def test_claim_contract_status_refuses_stale_transition(client, engine):
    cid = _approved_contract(client, engine)
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        # Simulate the loser of a race: expected state no longer matches.
        assert models.claim_contract_status(s, contract, ("draft",), "approved") is False
        s.rollback()
        assert s.get(models.Contract, cid).status == "approved"


# ---------- renewal ----------

def test_renewal_skips_inactive_customer(client, engine, monkeypatch):
    cid, token = _sent_with_token(client, engine, monkeypatch)
    client.cookies.clear()
    client.post(f"/sign/{token}", data={"signed_name": "Ada", "agree": "yes"})
    client.post("/login", data={"username": "owner", "password": "testpass"})
    with Session(engine) as s:
        contract = s.get(models.Contract, cid)
        season = contract.season_year
        contract.pool.property.customer.active = False
        s.commit()
    r = client.get(f"/contracts/renew?from_season={season}&to_season={season + 1}")
    assert "Nothing to renew" in r.text or "0" in r.text
    client.post("/contracts/renew", data={"from_season": str(season),
                                          "to_season": str(season + 1)},
                follow_redirects=False)
    with Session(engine) as s:
        drafts = s.scalars(select(models.Contract).where(
            models.Contract.season_year == season + 1)).all()
        assert drafts == []


# ---------- settings ----------

def test_save_public_url_validates_and_persists(client, engine):
    r = client.post("/settings/public-url", data={"public_base_url": "ftp://nope"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "public_base_url", "") == ""  # rejected

    client.post("/settings/public-url",
                data={"public_base_url": "https://pool.example.com/"},
                follow_redirects=False)
    with Session(engine) as s:
        assert models.get_setting(s, "public_base_url", "") == "https://pool.example.com"

    client.post("/settings/public-url", data={"public_base_url": ""},
                follow_redirects=False)
    with Session(engine) as s:
        assert models.get_setting(s, "public_base_url", "x") == ""  # cleared


# ---------- public body cap ----------

def test_oversized_public_post_is_rejected(client):
    r = client.post("/sign/whatever", content=b"a" * (65 * 1024),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    r = client.post("/login", content=b"a" * (65 * 1024),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


# ---------- hidden template blocks reach the PDF ----------

def test_hidden_rates_block_is_absent_from_rendered_pdf(client, engine):
    import json

    from pypdf import PdfReader

    with Session(engine) as s:
        tpl = {"title": "Agreement", "blocks": [
            {"kind": "text", "title": "Intro", "body": "Hello.", "when": None},
            {"kind": "rates", "visible": False},
        ]}
        models.set_setting(s, "contract_template", json.dumps(tpl))
        _, _, pool = _seed_pool(s)
        pool_id = pool.id
        s.commit()
    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    client.post(f"/contracts/{cid}/approve", follow_redirects=False)
    pdf = client.get(f"/contracts/{cid}/pdf").content
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Service Rates" not in text
    assert "Intro" in text


# ---------- retention ----------

def test_prune_history_deletes_only_expired_rows(client, engine):
    with Session(engine) as s:
        old = models.utcnow() - timedelta(days=800)
        stale = models.utcnow() - timedelta(days=100)
        s.add(models.EmailOutbox(to_addr="a@b.c", subject="s", body="b",
                                 status="sent", created_at=stale))
        s.add(models.EmailOutbox(to_addr="a@b.c", subject="s", body="b",
                                 status="pending", created_at=stale))
        s.add(models.EmailOutbox(to_addr="a@b.c", subject="s", body="b",
                                 status="sent"))
        s.add(models.AuditLog(action="x", detail="old", at=old))
        s.add(models.AuditLog(action="x", detail="new"))
        s.commit()
        outbox_deleted, audit_deleted = prune_history(s)
        s.commit()
        assert outbox_deleted == 1  # settled + old only; pending survives any age
        assert audit_deleted == 1
        assert s.scalar(select(models.EmailOutbox.status).where(
            models.EmailOutbox.created_at < models.utcnow() - timedelta(days=30))) == "pending"


# ---------- log hygiene ----------

def test_redact_path_strips_sign_token():
    assert redact_path("/sign/abc123XYZ") == "/sign/[token]"
    assert redact_path("/sign/abc123XYZ/pdf") == "/sign/[token]/pdf"
    assert redact_path("/contracts/5") == "/contracts/5"


# ---------- atomic_write ----------

def test_atomic_write_leaves_no_tmp_sidecars(tmp_path):
    target = str(tmp_path / "out.pdf")
    atomic_write(target, b"one")
    atomic_write(target, b"two")
    assert open(target, "rb").read() == b"two"
    assert [f for f in os.listdir(tmp_path) if f != "out.pdf"] == []

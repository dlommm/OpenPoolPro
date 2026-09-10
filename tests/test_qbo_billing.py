from copy import deepcopy
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, qbo_api
from app import qbo_billing as billing
from app.db import Base


@pytest.fixture
def db(engine, monkeypatch):
    monkeypatch.setattr(qbo_api, "query_refund_receipts", lambda *args: [])
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for key, value in {
            "qbo_billing_enabled": "1",
            "qbo_refresh_token": "test-refresh",
            "qbo_billing_scope": "sandbox:123",
            "qbo_environment": "sandbox",
            "qbo_realm_id": "123",
            "qbo_item_scope": "sandbox:123",
            "qbo_item_service_id": "10",
            "qbo_item_chem_id": "20",
        }.items():
            models.set_setting(session, key, value)
        owner = models.User(name="Owner", role="admin")
        customer = models.Customer(
            name="Customer", email="customer@example.com", qbo_customer_id="5", qbo_customer_scope="sandbox:123"
        )
        session.add_all([owner, customer])
        session.flush()
        prop = models.Property(customer_id=customer.id, label="Home", address="1 Main St", state="NJ")
        session.add(prop)
        session.flush()
        session.add(models.Visit(property_id=prop.id, status="done", date=date(2026, 8, 10), rate_cents=10000))
        session.commit()
        yield session


def prepare(db):
    assert billing.prepare_month(db, 2026, 8, 1) == (1, 0)
    return db.scalar(select(models.QBODraft))


def approve(db):
    draft = prepare(db)
    return billing.approve_draft(db, draft.id, draft.revision, db.get(models.User, 1))


def remote(draft, **updates):
    raw = deepcopy(draft.approved_snapshot["payload"])
    raw.update(
        Id="99", SyncToken="0", DocNumber="1001", TotalAmt=draft.total_cents / 100, Balance=draft.total_cents / 100
    )
    raw.update(updates)
    return raw


def test_preparation_excludes_cash_and_reserves_moved_sources(db):
    db.add(models.Visit(property_id=1, status="done", date=date(2026, 8, 11), rate_cents=3000, paid_cash=True))
    db.add(
        models.Job(
            property_id=1,
            title="Paid repair",
            status="done",
            completed_at=datetime(2026, 8, 12),
            labor_cents=5000,
            paid_cash=True,
        )
    )
    db.commit()
    draft = prepare(db)
    assert draft.total_cents == 10000
    assert draft.source_keys == ["visit:1"]
    db.get(models.Visit, 1).date = date(2026, 9, 1)
    db.commit()
    assert billing.prepare_month(db, 2026, 9, 1) == (0, 0)


def test_legacy_invoice_never_rebilled(db):
    db.add(
        models.Invoice(
            number="old", customer_id=1, period_year=2026, period_month=8, total_cents=10000, pdf_filename="old.pdf"
        )
    )
    db.commit()
    assert billing.prepare_month(db, 2026, 8, 1) == (0, 1)


def test_edit_invalidates_approval_and_stale_revision_rejected(db):
    draft = approve(db)
    lines = deepcopy(draft.lines)
    lines[0]["amount_cents"] = 9000
    billing.edit_draft(db, draft.id, 1, lines, draft.email, draft.due_date, user_id=1)
    assert draft.status == "draft"
    assert not draft.approved_snapshot
    assert draft.total_cents == 9000
    with pytest.raises(billing.BillingError, match="changed"):
        billing.approve_draft(db, draft.id, 1, db.get(models.User, 1))


def test_permission_hold_and_scope_checks(db):
    draft = prepare(db)
    tech = models.User(name="Tech", role="tech")
    db.add(tech)
    db.commit()
    with pytest.raises(billing.BillingError, match="permission"):
        billing.approve_draft(db, draft.id, 1, tech)
    billing.edit_draft(db, draft.id, 1, draft.lines, draft.email, draft.due_date, hold=True)
    with pytest.raises(billing.BillingError, match="hold"):
        billing.approve_draft(db, draft.id, 2, db.get(models.User, 1))
    models.set_setting(db, "qbo_realm_id", "456")
    db.commit()
    with pytest.raises(billing.BillingError, match="company"):
        billing.approve_draft(db, draft.id, 2, db.get(models.User, 1))


def test_create_send_once_with_frozen_price(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft)
    calls = []
    monkeypatch.setattr(
        qbo_api,
        "create_invoice",
        lambda session, payload, request_id: calls.append((deepcopy(payload), request_id)) or raw,
    )
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: raw)
    monkeypatch.setattr(
        qbo_api, "send_invoice", lambda *args: calls.append("send") or dict(raw, EmailStatus="EmailSent")
    )
    db.get(models.Visit, 1).rate_cents = 99999
    db.commit()
    billing.submit_draft(db, draft.id)
    billing.submit_draft(db, draft.id)
    assert draft.status == "sent"
    assert len(calls) == 2
    assert calls[0][0]["Line"][0]["Amount"] == 100
    assert calls[0][1] == draft.request_id


def test_mismatch_requires_exact_displayed_review(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft, TotalAmt=107, Balance=107, TxnTaxDetail={"TotalTax": 7})
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: raw)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: raw)
    sends = []
    monkeypatch.setattr(qbo_api, "send_invoice", lambda *args: sends.append(1) or dict(raw, EmailStatus="EmailSent"))
    billing.submit_draft(db, draft.id)
    assert draft.status == "mismatch" and not sends
    with pytest.raises(billing.BillingError, match="displayed"):
        billing.approve_qbo_total(db, draft.id, 1, db.get(models.User, 1), "old-hash")
    billing.approve_qbo_total(db, draft.id, 1, db.get(models.User, 1), billing.invoice_review_hash(raw))
    billing.submit_draft(db, draft.id)
    assert draft.status == "sent" and len(sends) == 1
    assert draft.total_cents == 10700


def test_unknown_creation_searches_without_creating_again(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft)
    creates = []

    def timeout(*args):
        creates.append(1)
        raise qbo_api.QboError("timeout", outcome_unknown=True)

    monkeypatch.setattr(qbo_api, "create_invoice", timeout)
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: [])
    billing.submit_draft(db, draft.id)
    assert draft.status == "create_unknown"
    billing.submit_draft(db, draft.id)
    assert len(creates) == 1
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: [raw])
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: raw)
    monkeypatch.setattr(qbo_api, "send_invoice", lambda *args: dict(raw, EmailStatus="EmailSent"))
    billing.submit_draft(db, draft.id)
    assert draft.status == "sent" and len(creates) == 1


def test_unknown_delivery_is_never_resent(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft)
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: raw)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: raw)
    sends = []

    def timeout(*args):
        sends.append(1)
        raise qbo_api.QboError("timeout", outcome_unknown=True)

    monkeypatch.setattr(qbo_api, "send_invoice", timeout)
    billing.submit_draft(db, draft.id)
    billing.submit_draft(db, draft.id)
    assert draft.status == "delivery_unknown" and len(sends) == 1


def test_tax_flags_and_per_source_rounding(db):
    for key, value in {
        "sales_tax_enabled": "1",
        "sales_tax_default_ppm": "100000",
        "tax_weekly": "0",
        "tax_chems": "1",
    }.items():
        models.set_setting(db, key, value)
    chem = models.Chemical(name="Chlorine", unit="gal", cost_per_unit_cents=100)
    db.add(chem)
    db.flush()
    db.add(
        models.VisitChemical(
            visit_id=1, chemical_id=chem.id, amount=2, unit_cost_cents_frozen=100, total_cost_cents=200
        )
    )
    db.commit()
    draft = prepare(db)
    assert draft.tax_cents == 20
    assert draft.total_cents == 10220
    assert not draft.lines[0]["taxable"] and draft.lines[1]["taxable"]


def test_discard_releases_only_unsubmitted_sources(db):
    draft = approve(db)
    billing.discard_draft(db, draft.id, 1, 1)
    assert billing.prepare_month(db, 2026, 8, 1) == (1, 0)
    draft = db.scalar(select(models.QBODraft))
    draft.status = "creating"
    db.commit()
    with pytest.raises(billing.BillingError, match="unsubmitted"):
        billing.discard_draft(db, draft.id, 1, 1)


def test_full_reconciliation_payments_credits_reversal_and_sensitive_fields(db, monkeypatch):
    invoice = {"Id": "99", "CustomerRef": {"value": "5"}, "TotalAmt": 100, "Balance": 20, "DocNumber": "I1"}
    payment = {
        "Id": "P1",
        "CustomerRef": {"value": "5"},
        "TotalAmt": 50,
        "CreditCardPayment": {"CreditChargeInfo": {"CcNumber": "secret"}},
        "Line": [
            {"Amount": 80, "LinkedTxn": [{"TxnId": "99", "TxnType": "Invoice"}]},
            {"Amount": 30, "LinkedTxn": [{"TxnId": "C1", "TxnType": "CreditMemo"}]},
        ],
    }
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: [invoice])
    monkeypatch.setattr(qbo_api, "query_payments", lambda *args: [payment])
    monkeypatch.setattr(
        qbo_api, "query_credit_memos", lambda *args: [{"Id": "C1", "CustomerRef": {"value": "5"}, "TotalAmt": 30}]
    )
    billing.reconcile(db)
    mirror = db.scalar(select(models.QBOInvoiceMirror))
    assert (mirror.balance_cents, mirror.paid_cents, mirror.credit_cents) == (2000, 5000, 3000)
    txn = db.scalar(select(models.QBOTransactionMirror).where(models.QBOTransactionMirror.kind == "Payment"))
    assert "CreditCardPayment" not in txn.raw
    invoice["Balance"] = 100
    monkeypatch.setattr(qbo_api, "query_payments", lambda *args: [])
    monkeypatch.setattr(qbo_api, "query_credit_memos", lambda *args: [])
    billing.reconcile(db)
    assert mirror.balance_cents == 10000 and mirror.paid_cents == 0
    assert not db.scalars(select(models.QBOTransactionMirror)).all()


def test_failed_scan_does_not_erase_balances(db, monkeypatch):
    db.add(
        models.QBOInvoiceMirror(scope="sandbox:123", qbo_id="99", customer_id=1, total_cents=10000, balance_cents=5000)
    )
    db.commit()
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: [])

    def fail(*args):
        raise qbo_api.QboError("page 2 failed")

    monkeypatch.setattr(qbo_api, "query_payments", fail)
    with pytest.raises(qbo_api.QboError):
        billing.reconcile(db)
    mirror = db.scalar(select(models.QBOInvoiceMirror))
    assert mirror.balance_cents == 5000 and mirror.status != "unavailable"


def test_missing_balance_rejected_not_recorded_paid(db, monkeypatch):
    monkeypatch.setattr(
        qbo_api, "query_invoices", lambda *args: [{"Id": "99", "CustomerRef": {"value": "5"}, "TotalAmt": 100}]
    )
    monkeypatch.setattr(qbo_api, "query_payments", lambda *args: [])
    monkeypatch.setattr(qbo_api, "query_credit_memos", lambda *args: [])
    with pytest.raises(billing.BillingError, match="balance"):
        billing.reconcile(db)
    assert not db.scalars(select(models.QBOInvoiceMirror)).all()


def test_cash_collected_after_preparation_blocks_approval_and_send(db, monkeypatch):
    draft = prepare(db)
    db.get(models.Visit, 1).paid_cash = True
    db.commit()
    with pytest.raises(billing.BillingError, match="cash"):
        billing.approve_draft(db, draft.id, 1, db.get(models.User, 1))
    db.get(models.Visit, 1).paid_cash = False
    db.commit()
    billing.approve_draft(db, draft.id, 1, db.get(models.User, 1))
    db.get(models.Visit, 1).paid_cash = True
    db.commit()
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: pytest.fail("Must not bill paid work"))
    with pytest.raises(billing.BillingError, match="cash"):
        billing.submit_draft(db, draft.id)


def test_reassigned_invoice_moves_owner_and_unmapped_invoice_is_hidden(db, monkeypatch):
    other = models.Customer(name="Other", qbo_customer_id="6", qbo_customer_scope="sandbox:123")
    db.add(other)
    db.commit()
    raw = {
        "Id": "99",
        "CustomerRef": {"value": "5"},
        "TotalAmt": 100,
        "Balance": 20,
        "EmailStatus": "EmailSent",
        "InvoiceLink": "https://connect.intuit.com/pay",
    }
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: [raw])
    monkeypatch.setattr(qbo_api, "query_payments", lambda *args: [])
    monkeypatch.setattr(qbo_api, "query_credit_memos", lambda *args: [])
    billing.reconcile(db)
    mirror = db.scalar(select(models.QBOInvoiceMirror))
    assert mirror.customer_id == 1
    raw["CustomerRef"] = {"value": "6"}
    billing.reconcile(db)
    assert mirror.customer_id == other.id
    raw["CustomerRef"] = {"value": "unmapped"}
    billing.reconcile(db)
    assert mirror.status == "unavailable" and not mirror.email_status and not mirror.payment_link


def test_mixed_credit_multi_invoice_allocation_is_not_guessed(db, monkeypatch):
    invoices = [{"Id": qid, "CustomerRef": {"value": "5"}, "TotalAmt": 100, "Balance": 50} for qid in ("99", "100")]
    payment = {
        "Id": "p",
        "CustomerRef": {"value": "5"},
        "TotalAmt": 50,
        "Line": [
            {"Amount": 50, "LinkedTxn": [{"TxnId": "99", "TxnType": "Invoice"}]},
            {"Amount": 50, "LinkedTxn": [{"TxnId": "100", "TxnType": "Invoice"}]},
            {"Amount": 50, "LinkedTxn": [{"TxnId": "c", "TxnType": "CreditMemo"}]},
        ],
    }
    monkeypatch.setattr(qbo_api, "query_invoices", lambda *args: invoices)
    monkeypatch.setattr(qbo_api, "query_payments", lambda *args: [payment])
    monkeypatch.setattr(qbo_api, "query_credit_memos", lambda *args: [])
    billing.reconcile(db)
    rows = db.scalars(select(models.QBOInvoiceMirror)).all()
    assert all(not row.allocation_known and row.balance_cents == 5000 for row in rows)
    assert all(row.paid_cents == 0 and row.credit_cents == 0 for row in rows)


def test_same_total_changed_taxability_blocks_send(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft)
    raw["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] = "TAX"
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: raw)
    monkeypatch.setattr(qbo_api, "send_invoice", lambda *args: pytest.fail("Changed taxability must be reviewed"))
    billing.submit_draft(db, draft.id)
    assert draft.status == "mismatch"


def test_live_invoice_change_after_render_rejects_reapproval(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft, TotalAmt=107, Balance=107, TxnTaxDetail={"TotalTax": 7})
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: raw)
    billing.submit_draft(db, draft.id)
    shown_hash = billing.invoice_review_hash(raw)
    changed = dict(raw, SyncToken="1", TotalAmt=108, Balance=108)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: changed)
    with pytest.raises(billing.BillingError, match="changed"):
        billing.approve_qbo_total(db, draft.id, 1, db.get(models.User, 1), shown_hash)
    assert draft.status == "mismatch"
    assert db.scalar(select(models.QBOInvoiceMirror)).total_cents == 10800


def test_scope_bound_enablement_blocks_old_confirmation(db):
    prepare(db)
    models.set_setting(db, "qbo_billing_scope", "production:other")
    db.commit()
    with pytest.raises(billing.BillingError, match="settings"):
        billing.approve_draft(db, 1, 1, db.get(models.User, 1))


def test_known_retryable_rejection_reuses_request_id(db, monkeypatch):
    draft = approve(db)
    raw = remote(draft)
    ids = []

    def create(session, payload, request_id):
        ids.append(request_id)
        if len(ids) == 1:
            raise qbo_api.QboError("rate limit", status_code=429, retryable=True)
        return raw

    monkeypatch.setattr(qbo_api, "create_invoice", create)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda *args: raw)
    monkeypatch.setattr(qbo_api, "send_invoice", lambda *args: dict(raw, EmailStatus="EmailSent"))
    billing.submit_draft(db, draft.id)
    assert draft.status == "create_retry"
    billing.process_pending(db)
    assert draft.status == "sent" and ids == [draft.request_id, draft.request_id]


def test_stale_worker_cannot_claim_newer_creation_lease(db):
    draft = approve(db)
    with Session(db.bind) as other:
        stale = other.get(models.QBODraft, draft.id)
        assert billing._claim(db, draft, ("approved",), "creating")
        assert not billing._claim(other, stale, ("approved", "creating"), "creating")


def test_changed_remote_customer_is_never_published_to_original_customer(db, monkeypatch):
    draft = approve(db)
    other = models.Customer(name="Other", qbo_customer_id="6", qbo_customer_scope="sandbox:123")
    db.add(other)
    db.commit()
    raw = remote(draft, CustomerRef={"value": "6"}, EmailStatus="EmailSent")
    monkeypatch.setattr(qbo_api, "create_invoice", lambda *args: raw)
    billing.submit_draft(db, draft.id)
    assert draft.status == "mismatch"
    assert not db.scalars(select(models.QBOInvoiceMirror)).all()

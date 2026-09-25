from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, portal, qbo_api, qbo_billing
from app.routers.qbo_billing import safe_payment_link, sync_job


def _records(engine):
    with Session(engine) as s:
        for key, value in (("qbo_realm_id", "123"), ("qbo_environment", "sandbox")):
            models.set_setting(s, key, value)
        customer = models.Customer(
            name="Billing Customer", email="bill@example.test", qbo_customer_id="100", qbo_customer_scope="sandbox:123"
        )
        other = models.Customer(name="Private Customer", email="private@example.test")
        s.add_all([customer, other])
        s.flush()
        draft = models.QBODraft(
            customer_id=customer.id,
            scope="sandbox:123",
            period_year=2026,
            period_month=7,
            due_date=date(2026, 8, 15),
            email=customer.email,
            lines=[
                {
                    "description": "Service visit",
                    "amount_cents": 10000,
                    "source_key": "visit:1",
                    "item_kind": "service",
                    "taxable": False,
                    "tax_rate_ppm": 0,
                }
            ],
            total_cents=10000,
        )
        invoice = models.QBOInvoiceMirror(
            customer_id=customer.id,
            scope="sandbox:123",
            qbo_id="10",
            number="QBO-10",
            total_cents=10000,
            balance_cents=5000,
            paid_cents=5000,
            due_date=date(2026, 8, 15),
            status="partial",
            email_status="EmailSent",
        )
        private = models.QBOInvoiceMirror(
            customer_id=other.id,
            scope="sandbox:123",
            qbo_id="11",
            number="PRIVATE-11",
            total_cents=20000,
            balance_cents=20000,
            email_status="EmailSent",
        )
        unsent = models.QBOInvoiceMirror(
            customer_id=customer.id,
            scope="sandbox:123",
            qbo_id="12",
            number="UNSENT-12",
            total_cents=10000,
            balance_cents=10000,
            email_status="NotSet",
        )
        s.add_all([draft, invoice, private, unsent])
        s.commit()
        return customer.id, other.id, draft.id, invoice.id, private.id, unsent.id


def test_billing_access_permissions(client, tech_client, engine):
    ids = _records(engine)
    denied = tech_client(name="denied")
    assert denied.get("/billing").status_code == 403
    assert denied.post("/billing/prepare", data={"ym": "2026-07"}).status_code == 403
    viewer = tech_client(name="viewer", view_money=True)
    assert viewer.get("/billing").status_code == 200
    assert viewer.get(f"/billing/drafts/{ids[2]}").status_code == 200
    assert viewer.post(f"/billing/drafts/{ids[2]}/approve", data={"revision": 1}).status_code == 403
    approver = tech_client(name="approver", approve_billing=True)
    page = approver.get(f"/billing/drafts/{ids[2]}")
    assert page.status_code == 200
    assert "Approve and send for invoicing" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert approver.post("/billing/settings").status_code == 403


def test_stale_edit_does_not_replace_charges(client, engine):
    _, _, did, *_ = _records(engine)
    response = client.post(
        f"/billing/drafts/{did}/edit",
        data={
            "revision": 0,
            "due_date": "2026-08-15",
            "email": "bill@example.test",
            "description_0": "Changed",
            "amount_0": "1.00",
        },
    )
    assert response.status_code == 200
    with Session(engine) as s:
        draft = s.get(models.QBODraft, did)
        assert draft.total_cents == 10000
        assert draft.lines[0]["description"] == "Service visit"


def test_portal_scopes_sent_mirrors_and_pdf(client, engine, monkeypatch):
    cid, _, _, iid, private, unsent = _records(engine)
    with Session(engine) as s:
        token = portal.issue(s, cid)
        s.commit()
    page = client.get(f"/portal/{token}")
    assert page.status_code == 200
    assert "QBO-10" in page.text
    assert "PRIVATE-11" not in page.text
    assert "UNSENT-12" not in page.text
    assert client.get(f"/portal/{token}/quickbooks/{private}/pdf").status_code == 404
    assert client.get(f"/portal/{token}/quickbooks/{unsent}/pdf").status_code == 404
    monkeypatch.setattr(qbo_api, "connected", lambda session: True)
    monkeypatch.setattr(qbo_api, "invoice_pdf", lambda session, qid: b"%PDF-1.4 test")
    monkeypatch.setattr(qbo_api, "get_invoice", lambda session, qid: {"CustomerRef": {"value": "100"}})
    assert client.get(f"/portal/{token}/quickbooks/{iid}/pdf").content == b"%PDF-1.4 test"
    monkeypatch.setattr(qbo_api, "connected", lambda session: False)
    assert client.get(f"/portal/{token}/quickbooks/{iid}/pdf").content == b"%PDF-1.4 test"


def test_scope_switch_does_not_mix_financial_records(client, engine):
    _records(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_environment", "production")
        s.commit()
    assert "QBO-10" not in client.get("/billing").text


def test_reconciliation_survives_submission_failure(client, engine, monkeypatch):
    with Session(engine) as s:
        models.set_setting(s, "qbo_billing_enabled", "1")
        s.commit()
    monkeypatch.setattr(qbo_api, "connected", lambda s: True)

    def fail(s):
        raise qbo_billing.BillingError("Temporary failure")

    called = []
    monkeypatch.setattr(qbo_billing, "process_pending", fail)
    monkeypatch.setattr(qbo_billing, "reconcile", lambda s: called.append(True))
    sync_job(engine)
    assert called == [True]


def test_payment_links_reject_malformed_or_untrusted_hosts():
    for link in ("https://[", "javascript:alert(1)", "https://intuit.com.evil.test/pay", "https://user@intuit.com/pay"):
        assert safe_payment_link(link) == ""
    assert safe_payment_link("https://connect.intuit.com/pay") == "https://connect.intuit.com/pay"


def test_link_requires_preview_and_rejects_duplicate(client, engine, monkeypatch):
    cid, other, *_ = _records(engine)
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "40", "DisplayName": "Verified"})
    assert client.post(f"/billing/customers/{cid}/confirm-link").status_code == 409
    assert "Verified" in client.post(f"/billing/customers/{cid}/preview-link", data={"qbo_id": "40"}).text
    with Session(engine) as s:
        customer = s.get(models.Customer, other)
        customer.qbo_customer_id = "40"
        customer.qbo_customer_scope = "sandbox:123"
        s.commit()
    assert client.post(f"/billing/customers/{cid}/confirm-link").status_code == 409


def test_enable_rejects_unsupported_company(client, engine, monkeypatch):
    monkeypatch.setattr(qbo_api, "connected", lambda s: True)
    monkeypatch.setattr(qbo_api, "company_info", lambda s: {"Country": "CA"})
    monkeypatch.setattr(qbo_api, "preferences", lambda s: {"CurrencyPrefs": {"HomeCurrency": {"value": "CAD"}}})
    response = client.post("/billing/settings", data={"reviewed": "true", "enabled": "true"})
    assert "requires a verified United States company" in response.text
    with Session(engine) as s:
        assert models.get_setting(s, "qbo_billing_enabled", "0") == "0"


def test_portal_pdf_rejects_unavailable_or_reassigned_invoice(client, engine, monkeypatch):
    cid, _, _, iid, *_ = _records(engine)
    with Session(engine) as s:
        token = portal.issue(s, cid)
        s.commit()
    monkeypatch.setattr(qbo_api, "connected", lambda s: True)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda s, qid: {"CustomerRef": {"value": "foreign"}})
    assert client.get(f"/portal/{token}/quickbooks/{iid}/pdf").status_code == 409
    with Session(engine) as s:
        s.get(models.QBOInvoiceMirror, iid).status = "unavailable"
        s.commit()
    assert "QBO-10" not in client.get(f"/portal/{token}").text
    assert client.get(f"/portal/{token}/quickbooks/{iid}/pdf").status_code == 404


def test_pdf_rejects_changed_remote_version(client, engine, monkeypatch):
    _, _, _, iid, *_ = _records(engine)
    monkeypatch.setattr(qbo_api, "connected", lambda s: True)
    monkeypatch.setattr(qbo_api, "get_invoice", lambda s, qid: {"CustomerRef": {"value": "100"}, "SyncToken": "2"})
    assert client.get(f"/billing/invoices/{iid}/pdf").status_code == 409


def _reserve(engine, draft_id, key="visit:1"):
    """Give a draft a source reservation, the row that stops the same work
    being billed twice. Discarding is supposed to release it."""
    with Session(engine) as s:
        draft = s.get(models.QBODraft, draft_id)
        draft.source_keys = [key]
        s.add(models.QBOBillingSource(source_key=key, draft_id=draft_id))
        s.commit()


def test_discard_refuses_a_submitted_draft(client, engine):
    """The non-destructive rule at the HTTP layer: once work has reached
    QuickBooks, nothing in this app may delete the local record of it."""
    _, _, did, *_ = _records(engine)
    _reserve(engine, did)
    with Session(engine) as s:
        s.get(models.QBODraft, did).qbo_invoice_id = "55"
        s.commit()
    r = client.post(f"/billing/drafts/{did}/discard", data={"revision": 1})
    assert r.status_code == 200
    assert "Only an unsubmitted draft can be discarded" in r.text
    with Session(engine) as s:
        assert s.get(models.QBODraft, did) is not None
        assert s.get(models.QBOBillingSource, "visit:1") is not None


def test_discard_refuses_a_stale_revision(client, engine):
    _, _, did, *_ = _records(engine)
    _reserve(engine, did)
    r = client.post(f"/billing/drafts/{did}/discard", data={"revision": 0})
    assert "Only an unsubmitted draft can be discarded" in r.text
    with Session(engine) as s:
        assert s.get(models.QBODraft, did) is not None


def test_discard_releases_the_source_reservation(client, engine):
    """The one case where deleting is right -- and it has to take the
    reservation with it, or that work can never be billed again."""
    _, _, did, *_ = _records(engine)
    _reserve(engine, did)
    r = client.post(f"/billing/drafts/{did}/discard", data={"revision": 1})
    assert "Unsubmitted draft discarded" in r.text
    with Session(engine) as s:
        assert s.get(models.QBODraft, did) is None
        assert s.get(models.QBOBillingSource, "visit:1") is None


def test_approve_stops_at_the_billing_switch_without_submitting(client, engine):
    """Approval runs the enable gate first. A refusal has to leave the draft
    exactly as it was -- not half-approved, and certainly not submitted."""
    _, _, did, *_ = _records(engine)
    r = client.post(f"/billing/drafts/{did}/approve", data={"revision": 1})
    assert r.status_code == 200
    assert "Enable reviewed QuickBooks billing" in r.text
    with Session(engine) as s:
        draft = s.get(models.QBODraft, did)
        assert draft.status == "draft"
        assert draft.qbo_invoice_id == ""
        assert draft.approved_at is None


def test_retry_refuses_a_draft_that_was_never_approved(client, engine):
    """Retry resubmits an approved draft. Without an approval snapshot there is
    nothing to resubmit, and it must not invent one."""
    _, _, did, *_ = _records(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_billing_enabled", "1")
        models.set_setting(s, "qbo_billing_scope", "sandbox:123")
        s.commit()
    r = client.post(f"/billing/drafts/{did}/retry")
    assert r.status_code == 200
    with Session(engine) as s:
        draft = s.get(models.QBODraft, did)
        assert draft.status == "draft"
        assert draft.qbo_invoice_id == ""


def test_prepare_reports_what_it_skipped(client, engine):
    """A customer already holding a draft for the period is skipped, not
    double-billed -- and the operator is told how many."""
    _records(engine)
    r = client.post("/billing/prepare", data={"ym": "2026-07"})
    assert r.status_code == 200
    assert "skipped" in r.text
    with Session(engine) as s:
        assert len(s.scalars(select(models.QBODraft)).all()) == 1


def test_prepare_refuses_without_a_quickbooks_connection(client, engine):
    _records(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_realm_id", "")
        s.commit()
    r = client.post("/billing/prepare", data={"ym": "2026-07"})
    assert "Connect QuickBooks before preparing billing" in r.text


def test_sync_route_reports_reconciled_counts(client, engine, monkeypatch):
    _records(engine)
    monkeypatch.setattr(qbo_billing, "reconcile", lambda s: {"invoices": 3, "payments": 2, "credits": 1})
    r = client.post("/billing/sync")
    assert "Reconciled 3 invoices, 2 payments, and 1 credits" in r.text


def test_sync_route_surfaces_a_failure_instead_of_a_blank_page(client, engine, monkeypatch):
    _records(engine)

    def boom(session):
        raise qbo_billing.BillingError("QuickBooks is unavailable right now.")

    monkeypatch.setattr(qbo_billing, "reconcile", boom)
    assert "QuickBooks is unavailable right now." in client.post("/billing/sync").text


def test_enabling_delivery_requires_the_review_checkbox(client, engine, monkeypatch):
    _records(engine)
    monkeypatch.setattr(qbo_api, "connected", lambda s: True)
    r = client.post("/billing/settings", data={"enabled": "true"})
    assert "confirm billing setup before enabling" in r.text
    with Session(engine) as s:
        assert models.get_setting(s, "qbo_billing_enabled", "0") == "0"


def test_disabling_delivery_always_saves(client, engine):
    """Turning delivery off is the safe direction: it must never be blocked by
    a connection the operator is trying to get away from."""
    _records(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_billing_enabled", "1")
        s.commit()
    assert "Billing settings saved." in client.post("/billing/settings", data={}).text
    with Session(engine) as s:
        assert models.get_setting(s, "qbo_billing_enabled", "1") == "0"


def test_confirm_link_refuses_a_customer_that_changed_since_the_preview(client, engine, monkeypatch):
    """The operator approves what they were shown. If the remote record moved
    between preview and confirm, the approval no longer means anything."""
    _, other, *_ = _records(engine)
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "40", "DisplayName": "Before"})
    client.post(f"/billing/customers/{other}/preview-link", data={"qbo_id": "40"})
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "40", "DisplayName": "After"})
    r = client.post(f"/billing/customers/{other}/confirm-link")
    assert r.status_code == 409
    assert "changed" in r.text
    with Session(engine) as s:
        assert s.get(models.Customer, other).qbo_customer_id == ""


def test_confirm_link_refuses_to_move_an_established_mapping(client, engine, monkeypatch):
    """Repointing a customer who already has verified billing history is how
    outstanding invoices get stranded against the wrong QuickBooks record."""
    cid, *_ = _records(engine)  # already mapped to QuickBooks id 100
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "41", "DisplayName": "Somebody Else"})
    client.post(f"/billing/customers/{cid}/preview-link", data={"qbo_id": "41"})
    r = client.post(f"/billing/customers/{cid}/confirm-link")
    assert r.status_code == 409
    assert "already has a verified mapping" in r.text
    with Session(engine) as s:
        assert s.get(models.Customer, cid).qbo_customer_id == "100"


def test_confirm_link_records_the_match_and_audits_it(client, engine, monkeypatch):
    _, other, *_ = _records(engine)
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "40", "DisplayName": "Verified"})
    client.post(f"/billing/customers/{other}/preview-link", data={"qbo_id": "40"})
    assert "QuickBooks customer match confirmed." in client.post(f"/billing/customers/{other}/confirm-link").text
    with Session(engine) as s:
        customer = s.get(models.Customer, other)
        assert (customer.qbo_customer_id, customer.qbo_customer_scope) == ("40", "sandbox:123")
        entries = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "qbo.customer_linked")).all()
        assert len(entries) == 1 and "qbo_id=40" in entries[0].detail


def test_a_confirm_cannot_be_replayed(client, engine, monkeypatch):
    """The preview is consumed on confirm, so a resubmitted form cannot quietly
    relink a customer a second time."""
    _, other, *_ = _records(engine)
    monkeypatch.setattr(qbo_api, "get_customer", lambda s, qid: {"Id": "40", "DisplayName": "Verified"})
    client.post(f"/billing/customers/{other}/preview-link", data={"qbo_id": "40"})
    assert client.post(f"/billing/customers/{other}/confirm-link").status_code == 200
    assert client.post(f"/billing/customers/{other}/confirm-link").status_code == 409

"""The AR screens and the payment / invoice-email actions, end to end."""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mailer, models


def _billable_month(engine, client, *, total_rate=20000):
    """A completed visit -> a generated invoice, the way the app really does it."""
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True, email="acme@example.com")
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", address="1 St", active=True,
                            weekday=0, frequency="weekly", route_order=1,
                            rate_override_cents=total_rate)
        s.add(p)
        s.flush()
        s.add(models.Visit(property_id=p.id, date=today, status="done",
                           rate_cents=total_rate))
        s.commit()
    client.post("/invoices/generate", data={"ym": f"{today.year}-{today.month:02d}"},
                follow_redirects=False)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        return inv.id, inv.number, inv.total_cents


def test_generated_invoices_get_a_due_date_from_the_terms(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "invoice_net_days", "30")
        s.commit()
    iid, _, _ = _billable_month(engine, client)
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.due_date is not None
        # Terms run from the END of the billing period, not the generation day.
        from app import qbo
        _, last = qbo._month_range(inv.period_year, inv.period_month)
        assert inv.due_date == last + timedelta(days=30)


def test_recording_a_payment_clears_the_balance(client, engine):
    iid, number, total = _billable_month(engine, client)
    r = client.post(f"/invoices/{iid}/payments",
                    data={"amount_dollars": f"{total / 100:.2f}", "method": "check",
                          "reference": "1234"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.payment_state == "paid" and inv.balance_cents == 0
        assert inv.payments[0].reference == "1234"


def test_payment_amount_must_be_positive(client, engine):
    iid, _, _ = _billable_month(engine, client)
    r = client.post(f"/invoices/{iid}/payments", data={"amount_dollars": "0"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=payment" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).amount_paid_cents == 0


def test_a_garbage_amount_does_not_500(client, engine):
    iid, _, _ = _billable_month(engine, client)
    r = client.post(f"/invoices/{iid}/payments", data={"amount_dollars": "twelve"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).amount_paid_cents == 0


def test_a_payment_can_be_backed_out_and_is_audited(client, engine):
    iid, _, total = _billable_month(engine, client)
    client.post(f"/invoices/{iid}/payments", data={"amount_dollars": "50.00"},
                follow_redirects=False)
    with Session(engine) as s:
        pid = s.scalars(select(models.Payment)).one().id
    client.post(f"/payments/{pid}/delete", follow_redirects=False)
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).amount_paid_cents == 0
        actions = {a.action for a in s.scalars(select(models.AuditLog)).all()}
        assert "invoice.payment" in actions and "invoice.payment_deleted" in actions


def test_ar_page_lists_what_is_owed(client, engine):
    iid, number, total = _billable_month(engine, client)
    body = client.get("/ar").text
    assert number in body and "Acme" in body

    client.post(f"/invoices/{iid}/payments",
                data={"amount_dollars": f"{total / 100:.2f}"}, follow_redirects=False)
    body = client.get("/ar").text
    assert "Nothing outstanding" in body


def test_ar_payment_returns_to_the_ar_page(client, engine):
    """The owner works down the aging report; a payment must not bounce them
    back to the invoice register."""
    iid, _, _ = _billable_month(engine, client)
    r = client.post(f"/invoices/{iid}/payments",
                    data={"amount_dollars": "10.00", "back": "ar"}, follow_redirects=False)
    assert r.headers["location"].startswith("/ar")


def test_emailing_an_invoice_attaches_the_pdf_and_marks_it_sent(client, engine, monkeypatch):
    iid, number, _ = _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()

    captured = {}

    def fake_send(to_addr, subject, body, conf=None, attachments=None, html=None):
        captured.update(to=to_addr, subject=subject, body=body, attachments=attachments)

    monkeypatch.setattr(mailer, "send_message", fake_send)
    r = client.post(f"/invoices/{iid}/email", follow_redirects=False)
    assert r.status_code == 303

    assert captured["to"] == "acme@example.com"
    assert number in captured["subject"]
    assert captured["attachments"] and captured["attachments"][0][1] == f"{number}.pdf"
    assert captured["attachments"][0][0].startswith(b"%PDF")
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.status == "sent" and inv.sent_at is not None


def test_a_failed_send_leaves_the_invoice_in_draft(client, engine, monkeypatch):
    """Marking sent on a send that did not happen would hide it from the
    batch retry forever."""
    iid, _, _ = _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()

    def boom(*a, **kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(mailer, "send_message", boom)
    r = client.post(f"/invoices/{iid}/email", follow_redirects=False)
    assert "error=send" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).status == "draft"


def test_emailing_without_smtp_configured_reports_it(client, engine):
    iid, _, _ = _billable_month(engine, client)
    r = client.post(f"/invoices/{iid}/email", follow_redirects=False)
    assert "error=email" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).status == "draft"


def test_batch_email_sends_every_draft(client, engine, monkeypatch):
    from app.routers.today import business_today
    today = business_today()
    _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()
    sent = []
    monkeypatch.setattr(mailer, "send_message",
                        lambda *a, **kw: sent.append(a[0]))
    r = client.post("/invoices/email-batch",
                    data={"ym": f"{today.year}-{today.month:02d}"}, follow_redirects=False)
    assert "sent=1" in r.headers["location"] and len(sent) == 1


def test_reminder_is_queued_through_the_outbox(client, engine):
    iid, number, _ = _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        inv = s.get(models.Invoice, iid)
        inv.due_date = date(2020, 1, 1)      # long overdue
        s.commit()

    r = client.post(f"/invoices/{iid}/reminder", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.to_addr == "acme@example.com"
        assert number in row.subject
        assert "past due" in row.body


def test_reminder_refused_on_a_settled_invoice(client, engine):
    iid, _, total = _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.commit()
    client.post(f"/invoices/{iid}/payments",
                data={"amount_dollars": f"{total / 100:.2f}"}, follow_redirects=False)
    r = client.post(f"/invoices/{iid}/reminder", follow_redirects=False)
    assert r.status_code == 400


def test_ar_is_admin_only(client, tech_client, engine):
    _billable_month(engine, client)
    tech = tech_client(name="nobody")
    assert tech.get("/ar", follow_redirects=False).status_code == 403
    assert tech.post("/invoices/1/payments",
                     data={"amount_dollars": "10"},
                     follow_redirects=False).status_code == 403

"""Approval-first invoice reminders, revalidated immediately before delivery."""
import hashlib
import json

from sqlalchemy import select

from app import mailer, models, receivables
from app.timefmt import business_today, local_date

DEFAULT_STAGES = (7, 14, 30)


def stages(session):
    raw = models.get_setting(session, "followup_days", "7,14,30")
    try:
        values = sorted({int(v.strip()) for v in raw.split(",")})
        return values if 1 <= len(values) <= 6 and all(1 <= v <= 365 for v in values) else list(DEFAULT_STAGES)
    except ValueError:
        return list(DEFAULT_STAGES)


def message(session, invoice):
    company = models.get_setting(session, "business_name", "OpenPoolPro") or "OpenPoolPro"
    subject = f"{company} — reminder for invoice {invoice.number}"
    body = (f"Hello {invoice.customer.name},\n\n"
            f"Our records show ${invoice.balance_cents / 100:,.2f} outstanding on invoice {invoice.number}, "
            f"due {invoice.due_date.strftime('%B %-d, %Y') if invoice.due_date else 'on receipt'}.\n\n"
            "If you have already paid, thank you — please reply with the payment details so we can update "
            f"our records. For payment arrangements or questions, please reply to this email.\n\n{company}")
    return subject, body


def snapshot(invoice, stage, subject, body):
    return hashlib.sha256(json.dumps([invoice.id, stage, invoice.balance_cents,
        invoice.customer.email, invoice.status, subject, body]).encode()).hexdigest()


def candidates(session, today):
    history = session.scalars(select(models.InvoiceFollowup)).all()
    sent_or_queued = {(r.invoice_id, r.stage) for r in history if r.outbox.status != "canceled"}
    # One reminder per invoice per day even if the thresholds are changed.
    today_sent = {r.invoice_id for r in history
                  if local_date(r.created_at) == today and r.outbox.status != "canceled"}
    rows = []
    for inv in receivables.open_invoices(session):
        if inv.status != "sent" or not inv.customer.active or not inv.customer.email or inv.id in today_sent:
            continue
        eligible = [n for n in stages(session) if inv.days_overdue(today) >= n]
        if not eligible:
            continue
        stage = max(eligible)
        if (inv.id, stage) in sent_or_queued:
            continue
        subject, body = message(session, inv)
        rows.append({"invoice": inv, "stage": stage, "subject": subject, "body": body,
                     "snapshot": snapshot(inv, stage, subject, body)})
    return rows


def approve(session, row, user_id):
    inv = row["invoice"]
    old = session.scalars(select(models.InvoiceFollowup).where(
        models.InvoiceFollowup.invoice_id == inv.id, models.InvoiceFollowup.stage == row["stage"])).first()
    if old is not None:
        # Never recycle already approved work. A canceled preview may be rebuilt.
        if old.outbox.status != "canceled":
            return False
        session.delete(old)
        session.flush()
    outbox = mailer.queue(session, inv.customer.email, row["subject"], row["body"], inv.customer_id)
    session.add(models.InvoiceFollowup(invoice_id=inv.id, stage=row["stage"],
        balance_cents=inv.balance_cents, due_date=inv.due_date, outbox_id=outbox.id, approved_by_id=user_id))
    return True


def may_deliver(session, outbox):
    followup = session.scalars(select(models.InvoiceFollowup).where(
        models.InvoiceFollowup.outbox_id == outbox.id)).first()
    if followup is None:
        return True
    inv = session.get(models.Invoice, followup.invoice_id, populate_existing=True)
    session.expire(inv, ["payments", "customer"])
    return (inv.status == "sent" and inv.customer.active and inv.balance_cents > 0
            and inv.due_date == followup.due_date and inv.days_overdue(business_today()) >= followup.stage
            and inv.balance_cents == followup.balance_cents and inv.customer.email == outbox.to_addr)

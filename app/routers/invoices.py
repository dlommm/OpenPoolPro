import io
import os
import zipfile
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import invoicing, mailer, models, receivables
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.main import templates
from app.routers.today import business_today
from app.timefmt import parse_ym

router = APIRouter(dependencies=[Depends(require_admin)])

STATUSES = ("draft", "sent", "void")


def _pdf_path(inv: models.Invoice) -> str:
    return os.path.join(invoicing._invoices_dir(), inv.pdf_filename)


def _cents(dollars: str) -> int | None:
    """Dollar string -> integer cents. Blank/malformed/negative -> None, so a
    typo is a rejected form rather than a 500 or a negative payment."""
    text = (dollars or "").strip().lstrip("$").replace(",", "")
    if not text:
        return None
    try:
        value = round(float(text) * 100)
    except ValueError:
        return None
    return value if value > 0 else None


def _date_or_today(s: str) -> date:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return business_today()


def _back(where: str, inv: models.Invoice, error: str | None = None):
    """Return to whichever screen the action was fired from.

    Payments are recorded from both the invoice register and the AR page; a
    hard-coded redirect would bounce the owner off the report they were
    working through.
    """
    target = "/ar" if where == "ar" else \
        f"/invoices?ym={inv.period_year}-{inv.period_month:02d}"
    if error:
        target += ("&" if "?" in target else "?") + f"error={error}"
    return RedirectResponse(target, status_code=303)


def _company_name(session: Session) -> str:
    from app.routers.company import company_info
    return company_info(session).get("name") or "Your pool service"


def _send_invoice_email(session: Session, inv: models.Invoice,
                        user_id: int) -> tuple[bool, str | None]:
    """Attach the PDF and send. Returns (ok, reason).

    Sent directly rather than through the outbox because the PDF attachment
    would have to be re-read at flush time — the outbox stores a text body
    only. A failure leaves the invoice in draft so it is retried, rather than
    marking it sent on a send that did not happen.
    """
    customer = inv.customer
    path = _pdf_path(inv)
    if not customer.email or not mailer.smtp_configured(session):
        return False, "email"
    if not os.path.isfile(path):
        return False, "pdf"
    with open(path, "rb") as f:
        pdf_bytes = f.read()
    period = date(inv.period_year, inv.period_month, 1).strftime("%B %Y")
    body = (f"Hello {customer.name},\n\n"
            f"Attached is invoice {inv.number} for {period}, "
            f"totalling ${inv.total_cents / 100:,.2f}"
            + (f", due {inv.due_date.isoformat()}" if inv.due_date else "")
            + ".\n\nThank you for your business.\n\n"
            f"{_company_name(session)}")
    try:
        mailer.send_message(customer.email, f"Invoice {inv.number} — {period}", body,
                            conf=mailer.smtp_settings(session),
                            attachments=[(pdf_bytes, f"{inv.number}.pdf")])
    except Exception:
        return False, "send"
    if inv.status == "draft":
        inv.status = "sent"
    inv.sent_at = models.utcnow()
    models.audit(session, user_id, "invoice.emailed", detail=inv.number)
    return True, None


@router.get("/invoices")
def invoices_page(request: Request, ym: str | None = None, status: str | None = None,
                   session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    status_filter = status if status in STATUSES else None
    q = select(models.Invoice).where(models.Invoice.period_year == year,
                                      models.Invoice.period_month == month)
    if status_filter:
        q = q.where(models.Invoice.status == status_filter)
    invoices = session.scalars(q.order_by(models.Invoice.number).options(
        selectinload(models.Invoice.payments),
        selectinload(models.Invoice.customer))).all()
    rows = [{"invoice": inv, "pdf_missing": not os.path.isfile(_pdf_path(inv))}
            for inv in invoices]
    return templates.TemplateResponse(request, "invoices.html", {
        "rows": rows, "year": year, "month": month,
        "ym": f"{year}-{month:02d}", "status": status_filter, "statuses": STATUSES,
        "today": business_today(), "methods": models.PAYMENT_METHODS,
        "can_email": mailer.smtp_configured(session)})


@router.get("/ar")
def ar_page(request: Request, session: Session = Depends(get_session)):
    """Accounts receivable aging — who owes what, and how late."""
    today = business_today()
    rows = receivables.by_customer(session, today)
    return templates.TemplateResponse(request, "ar.html", {
        "rows": rows, "totals": receivables.totals(rows), "today": today,
        "buckets": [label for label, _ in receivables.AGING_BUCKETS],
        "methods": models.PAYMENT_METHODS,
        "can_email": mailer.smtp_configured(session),
        "net_days": receivables.default_net_days(session)})


@router.post("/invoices/{iid}/payments")
def record_payment(request: Request, iid: int, amount_dollars: str = Form(""),
                    received_on: str = Form(""), method: str = Form("check"),
                    reference: str = Form(""), note: str = Form(""),
                    back: str = Form(""), session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    amount = _cents(amount_dollars)
    received = _date_or_today(received_on)
    payment = receivables.record_payment(
        session, inv, amount_cents=amount or 0, received_on=received, method=method,
        reference=reference, note=note, user_id=request.state.user.id)
    if payment is None:
        return _back(back, inv, error="payment")
    models.audit(session, request.state.user.id, "invoice.payment",
                 detail=f"{inv.number} +{amount} cents ({method})")
    session.commit()
    return _back(back, inv)


@router.post("/payments/{pid}/delete")
def delete_payment(request: Request, pid: int, back: str = Form(""),
                    session: Session = Depends(get_session)):
    """Back out a mis-entry (or a bounced check). Audited, because this moves
    money on the books."""
    payment = get_or_404(session, models.Payment, pid)
    inv = payment.invoice
    models.audit(session, request.state.user.id, "invoice.payment_deleted",
                 detail=f"{inv.number} -{payment.amount_cents} cents")
    session.delete(payment)
    session.commit()
    return _back(back, inv)


@router.post("/invoices/{iid}/email")
def email_invoice(request: Request, iid: int, back: str = Form(""),
                   session: Session = Depends(get_session)):
    """Send the invoice PDF to the customer and mark it sent.

    Mirrors the contract-emailing path (app/routers/contracts.py), which was
    the only place in the app that ever attached a PDF to an email — invoices
    were generated, zipped, and then left for the owner to send by hand.
    """
    inv = get_or_404(session, models.Invoice, iid)
    if inv.status == "void":
        raise HTTPException(status_code=400)
    ok, reason = _send_invoice_email(session, inv, request.state.user.id)
    session.commit()
    return _back(back, inv, error=None if ok else reason)


@router.post("/invoices/email-batch")
def email_batch(request: Request, ym: str = Form(""),
                 session: Session = Depends(get_session)):
    """Email every un-sent, non-void invoice for a period."""
    year, month = parse_ym(ym)
    invoices = session.scalars(select(models.Invoice).where(
        models.Invoice.period_year == year, models.Invoice.period_month == month,
        models.Invoice.status == "draft").options(
            selectinload(models.Invoice.customer))).all()
    sent = failed = 0
    for inv in invoices:
        ok, _ = _send_invoice_email(session, inv, request.state.user.id)
        sent, failed = (sent + 1, failed) if ok else (sent, failed + 1)
    session.commit()
    return RedirectResponse(
        f"/invoices?ym={year}-{month:02d}&sent={sent}&failed={failed}", status_code=303)


@router.post("/invoices/{iid}/reminder")
def send_reminder(request: Request, iid: int, back: str = Form(""),
                   session: Session = Depends(get_session)):
    """Nudge an overdue invoice. Queued through the outbox like every other
    customer email, so a transient SMTP failure retries instead of vanishing."""
    inv = get_or_404(session, models.Invoice, iid)
    customer = inv.customer
    if inv.balance_cents <= 0 or inv.status == "void":
        raise HTTPException(status_code=400)
    if not customer.email or not mailer.smtp_configured(session):
        return _back(back, inv, error="email")

    days = inv.days_overdue(business_today())
    company = _company_name(session)
    overdue_line = (f"It is now {days} day{'s' if days != 1 else ''} past due."
                    if days else "")
    body = (f"Hello {customer.name},\n\n"
            f"Invoice {inv.number} has an outstanding balance of "
            f"${inv.balance_cents / 100:,.2f}. {overdue_line}\n\n"
            "If you have already sent payment, thank you — please disregard "
            "this note.\n\n"
            f"{company}")
    mailer.queue(session, customer.email, f"Reminder: invoice {inv.number}", body,
                 customer_id=customer.id)
    models.audit(session, request.state.user.id, "invoice.reminder",
                 detail=f"{inv.number} ({days}d overdue)")
    session.commit()
    return _back(back, inv)


@router.post("/invoices/generate")
def invoices_generate(request: Request, ym: str = Form(""),
                       session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    created, skipped = invoicing.generate_month(session, year, month, request.state.user.id)
    models.audit(session, request.state.user.id, "invoice.generated",
                 detail=f"{year}-{month:02d} created={created}")
    session.commit()
    return RedirectResponse(f"/invoices?ym={year}-{month:02d}", status_code=303)


@router.get("/invoices/batch.zip")
def invoices_batch_zip(ym: str | None = None, session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    invoices = session.scalars(select(models.Invoice).where(
        models.Invoice.period_year == year, models.Invoice.period_month == month,
        models.Invoice.status != "void")).all()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for inv in invoices:
            path = _pdf_path(inv)
            if os.path.isfile(path):
                zf.write(path, arcname=inv.pdf_filename)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename=invoices-{year}-{month:02d}.zip"})


@router.get("/invoices/{iid}/pdf")
def invoice_pdf_route(iid: int, session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    path = _pdf_path(inv)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=f"{inv.number}.pdf")


@router.post("/invoices/{iid}/status")
def invoice_set_status(request: Request, iid: int, action: str = Form(...),
                        session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    if action not in ("sent", "void"):
        raise HTTPException(status_code=400)
    if inv.status == "void":
        raise HTTPException(status_code=400)  # void is terminal
    if action == "sent" and inv.status != "draft":
        raise HTTPException(status_code=400)  # only draft -> sent
    inv.status = action
    models.audit(session, request.state.user.id, "invoice.status",
                 detail=f"{inv.number} -> {action}")
    session.commit()
    return RedirectResponse(f"/invoices?ym={inv.period_year}-{inv.period_month:02d}", status_code=303)


@router.post("/invoices/{iid}/regenerate")
def invoice_regenerate(request: Request, iid: int, session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    if inv.status == "void":
        raise HTTPException(status_code=400)

    from app import invoice_pdf

    customer = get_or_404(session, models.Customer, inv.customer_id)
    data = invoicing.invoice_data(session, customer, inv.period_year, inv.period_month)
    data.number = inv.number
    inv.generated_at = models.utcnow()
    data.generated = inv.generated_at  # PDF date must match the committed row
    pdf_bytes = invoice_pdf.render_invoice(data)
    inv.total_cents = data.total_cents
    inv.subtotal_cents = data.subtotal_cents
    inv.tax_cents = data.tax_cents
    models.audit(session, request.state.user.id, "invoice.generated",
                 detail=f"regenerate {inv.number}")
    session.commit()
    # File write comes after the commit: if the write fails, the DB still
    # reflects the fresh total/generated_at and the register's "pdf missing"
    # tag flags it for a retry, rather than leaving a fresh PDF paired with
    # a stale DB row. The write itself is atomic (tmp file + os.replace), so
    # a crash mid-write can never leave a truncated PDF at the final path.
    path = _pdf_path(inv)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    os.replace(tmp_path, path)
    return RedirectResponse(f"/invoices?ym={inv.period_year}-{inv.period_month:02d}", status_code=303)

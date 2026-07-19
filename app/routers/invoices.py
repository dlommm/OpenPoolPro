import io
import os
import zipfile

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import invoicing, models
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.main import templates
from app.timefmt import parse_ym

router = APIRouter(dependencies=[Depends(require_admin)])

STATUSES = ("draft", "sent", "void")


def _pdf_path(inv: models.Invoice) -> str:
    return os.path.join(invoicing._invoices_dir(), inv.pdf_filename)


@router.get("/invoices")
def invoices_page(request: Request, ym: str | None = None, status: str | None = None,
                   session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    status_filter = status if status in STATUSES else None
    q = select(models.Invoice).where(models.Invoice.period_year == year,
                                      models.Invoice.period_month == month)
    if status_filter:
        q = q.where(models.Invoice.status == status_filter)
    invoices = session.scalars(q.order_by(models.Invoice.number)).all()
    rows = [{"invoice": inv, "pdf_missing": not os.path.isfile(_pdf_path(inv))}
            for inv in invoices]
    return templates.TemplateResponse(request, "invoices.html", {
        "rows": rows, "year": year, "month": month,
        "ym": f"{year}-{month:02d}", "status": status_filter, "statuses": STATUSES})


@router.post("/invoices/generate")
def invoices_generate(request: Request, ym: str = Form(""),
                       session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    invoicing.generate_month(session, year, month, request.state.user.id)
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
def invoice_set_status(iid: int, action: str = Form(...),
                        session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    if action not in ("sent", "void"):
        raise HTTPException(status_code=400)
    if inv.status == "void":
        raise HTTPException(status_code=400)  # void is terminal
    if action == "sent" and inv.status != "draft":
        raise HTTPException(status_code=400)  # only draft -> sent
    inv.status = action
    session.commit()
    return RedirectResponse(f"/invoices?ym={inv.period_year}-{inv.period_month:02d}", status_code=303)


@router.post("/invoices/{iid}/regenerate")
def invoice_regenerate(iid: int, session: Session = Depends(get_session)):
    inv = get_or_404(session, models.Invoice, iid)
    if inv.status == "void":
        raise HTTPException(status_code=400)

    from app import invoice_pdf

    customer = get_or_404(session, models.Customer, inv.customer_id)
    data = invoicing.invoice_data(session, customer, inv.period_year, inv.period_month)
    data.number = inv.number
    pdf_bytes = invoice_pdf.render_invoice(data)
    inv.total_cents = data.total_cents
    inv.generated_at = models.utcnow()
    session.commit()
    # File write comes after the commit: if this fails, the DB reflects the
    # fresh total and the register's "pdf missing" tag flags it for a retry,
    # rather than leaving a fresh PDF paired with a stale DB row.
    with open(_pdf_path(inv), "wb") as f:
        f.write(pdf_bytes)
    return RedirectResponse(f"/invoices?ym={inv.period_year}-{inv.period_month:02d}", status_code=303)

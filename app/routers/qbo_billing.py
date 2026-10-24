"""Review and approve billing, with locally retained QuickBooks records."""

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import config, models, qbo_api, qbo_billing, qbo_sync
from app.db import get_or_404, get_session
from app.deps import current_user, require_admin, require_perm
from app.fileio import atomic_write
from app.forms import cents
from app.templating import templates
from app.timefmt import business_today, parse_ym

router = APIRouter()


def billing_user(user=Depends(current_user)):
    if not (user.can("view_money") or user.can("approve_billing")):
        raise HTTPException(403)
    return user


def _back(message="", draft_id=None):
    target = f"/billing/drafts/{draft_id}" if draft_id else "/billing"
    return RedirectResponse(target + ("?" + urlencode({"message": message}) if message else ""), 303)


def _draft_ym(draft):
    return f"{draft.period_year}-{draft.period_month:02d}"


def _split_url(ym, draft_id=None, message=""):
    """The billing list with a draft open in the review pane."""
    query = {"ym": ym}
    if draft_id:
        query["draft"] = draft_id
    if message:
        query["message"] = message
    return "/billing?" + urlencode(query)


def _draft_back(message, draft_id, ym, back=""):
    """Where a draft action returns to. Forms in the desktop review pane send
    back=list so the owner stays on the list with the draft still open; the
    full review page (and any other value) keeps its own redirect. Only the
    fixed token is honoured, so this can never become an open redirect."""
    if back == "list":
        return RedirectResponse(_split_url(ym, draft_id, message), 303)
    return _back(message, draft_id)


def invoice_rows(session, customer_id=None, sent_only=False):
    scope = (
        qbo_api.connection_scope(session)
        if models.get_setting(session, "qbo_realm_id", "")
        else models.get_setting(session, "qbo_billing_sync_scope", "")
    )
    query = (
        select(models.QBOInvoiceMirror)
        .where(models.QBOInvoiceMirror.scope == scope)
        .options(selectinload(models.QBOInvoiceMirror.customer))
    )
    if customer_id is not None:
        query = query.where(models.QBOInvoiceMirror.customer_id == customer_id)
    if sent_only:
        query = query.where(
            models.QBOInvoiceMirror.email_status == "EmailSent",
            models.QBOInvoiceMirror.status.not_in(("unavailable", "void", "deleted")),
        )
    return session.scalars(
        query.order_by(models.QBOInvoiceMirror.txn_date.desc(), models.QBOInvoiceMirror.id.desc())
    ).all()


def safe_payment_link(value):
    try:
        parts = urlsplit(value or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    return (
        value
        if (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and (host == "intuit.com" or host.endswith(".intuit.com"))
        )
        else ""
    )


def invoice_context(session, customer_id=None, sent_only=False):
    rows = invoice_rows(session, customer_id, sent_only)
    scope = (
        qbo_api.connection_scope(session)
        if models.get_setting(session, "qbo_realm_id", "")
        else models.get_setting(session, "qbo_billing_sync_scope", "")
    )
    transactions = select(models.QBOTransactionMirror).where(models.QBOTransactionMirror.scope == scope)
    if customer_id is not None:
        transactions = transactions.where(models.QBOTransactionMirror.customer_id == customer_id)
    return {
        "qbo_transactions": session.scalars(transactions.order_by(models.QBOTransactionMirror.txn_date.desc())).all(),
        "qbo_invoices": rows,
        "qbo_balance_cents": sum(i.balance_cents for i in rows if i.status not in ("void", "deleted", "unavailable")),
        "qbo_today": business_today(),
        "qbo_stale_before": models.utcnow() - timedelta(minutes=30),
        "safe_payment_link": safe_payment_link,
    }


@router.get("/billing", dependencies=[Depends(billing_user)])
def index(request: Request, ym: str | None = None, draft: int | None = None, session: Session = Depends(get_session)):
    review = {}
    if draft is not None:
        selected = get_or_404(session, models.QBODraft, draft)
        # The open draft decides the month, so a stale or missing ym can never
        # show one month's list beside another month's draft.
        ym = _draft_ym(selected)
        review = {**_review_context(session, selected), "pane": True}
    year, month = parse_ym(ym)
    drafts = session.scalars(
        select(models.QBODraft)
        .where(models.QBODraft.period_year == year, models.QBODraft.period_month == month)
        .options(selectinload(models.QBODraft.customer))
        .order_by(models.QBODraft.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "billing/index.html",
        {
            "drafts": drafts,
            "ym": f"{year}-{month:02d}",
            "connected": qbo_api.connected(session),
            "enabled": models.get_setting(session, "qbo_billing_enabled", "") == "1",
            "last_sync": models.get_setting(session, "qbo_billing_last_sync", ""),
            "sync_error": models.get_setting(session, "qbo_billing_sync_error", ""),
            "mapping_customers": session.scalars(
                select(models.Customer).where(models.Customer.active).order_by(models.Customer.name)
            ).all()
            if request.state.user.role == "admin"
            else [],
            **invoice_context(session),
            **review,
        },
    )


@router.post("/billing/prepare", dependencies=[Depends(billing_user)])
def prepare(request: Request, ym: str = Form(...), session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    try:
        created, skipped = qbo_billing.prepare_month(session, year, month, request.state.user.id)
        session.commit()
    except qbo_billing.BillingError as exc:
        session.rollback()
        return _back(str(exc))
    return RedirectResponse(
        "/billing?"
        + urlencode(
            {
                "ym": f"{year}-{month:02d}",
                "message": f"Prepared {created} drafts; skipped {skipped} already reserved or ineligible customers.",
            }
        ),
        303,
    )


def _will_create_items(session, draft):
    """Billing kinds on this draft with no QuickBooks item yet. Read-only."""
    if draft.status not in ("draft", "held") or not qbo_api.connected(session):
        return []
    try:
        return qbo_sync.missing_items(session, draft.lines)
    except (qbo_sync.SyncError, qbo_api.QboError):
        # A review page must still render when QuickBooks is unreachable.
        return []


def _review_context(session, draft):
    """Everything the draft review partial needs, shared by the full page,
    the HTMX pane and the server-rendered split view."""
    mirror = session.scalar(
        select(models.QBOInvoiceMirror).where(
            models.QBOInvoiceMirror.scope == draft.scope, models.QBOInvoiceMirror.qbo_id == draft.qbo_invoice_id
        )
    )
    return {
        "review_hash": qbo_billing.invoice_review_hash(mirror.raw) if mirror else "",
        "draft": draft,
        "editable": draft.status in ("draft", "held", "approved"),
        # What approving would create in QuickBooks. Creating an item is
        # additive and needs no approval of its own, but an approver should
        # never be surprised by a record appearing in their books.
        "will_create_items": _will_create_items(session, draft),
        "connected": qbo_api.connected(session),
        "enabled": models.get_setting(session, "qbo_billing_enabled", "") == "1",
        "mirror": mirror,
        "service_item": models.get_setting(session, "qbo_item_service_id", ""),
        "chemical_item": models.get_setting(session, "qbo_item_chem_id", ""),
    }


@router.get("/billing/drafts/{draft_id}", dependencies=[Depends(billing_user)])
def detail(request: Request, draft_id: int, session: Session = Depends(get_session)):
    draft = get_or_404(session, models.QBODraft, draft_id)
    return templates.TemplateResponse(request, "billing/draft.html", _review_context(session, draft))


@router.get("/billing/drafts/{draft_id}/pane", dependencies=[Depends(billing_user)])
def pane(request: Request, draft_id: int, session: Session = Depends(get_session)):
    """The review as a fragment for the desktop list's right-hand pane. Only
    HTMX asks for the fragment; anything else (a pasted link, a crawler, a
    browser with scripts off) is sent to the full split view instead of an
    unstyled scrap of HTML."""
    draft = get_or_404(session, models.QBODraft, draft_id)
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(_split_url(_draft_ym(draft), draft.id), 303)
    return templates.TemplateResponse(
        request, "billing/_draft_review.html", {**_review_context(session, draft), "pane": True}
    )


@router.post("/billing/drafts/{draft_id}/edit", dependencies=[Depends(billing_user)])
async def edit(request: Request, draft_id: int, session: Session = Depends(get_session)):
    draft = get_or_404(session, models.QBODraft, draft_id)
    ym = _draft_ym(draft)
    form = await request.form()
    back = str(form.get("back", ""))
    try:
        revision = int(form.get("revision", ""))
        due = date.fromisoformat(str(form.get("due_date", "")))
        lines = []
        for idx, original in enumerate(draft.lines):
            amount = cents(str(form.get(f"amount_{idx}", "")), minimum=0)
            if amount is None:
                raise ValueError("Every charge must have a valid nonnegative amount.")
            lines.append(
                {**original, "description": str(form.get(f"description_{idx}", "")).strip(), "amount_cents": amount}
            )
        qbo_billing.edit_draft(
            session,
            draft_id,
            revision,
            lines,
            str(form.get("email", "")).strip(),
            due,
            form.get("hold") == "on",
            request.state.user.id,
        )
        session.commit()
    except (ValueError, qbo_billing.BillingError) as exc:
        session.rollback()
        return _draft_back(str(exc) or "Enter valid amounts, due date, and revision.", draft_id, ym, back)
    return _draft_back("Draft saved. Review the saved total before approving.", draft_id, ym, back)


@router.post("/billing/drafts/{draft_id}/approve", dependencies=[Depends(require_perm("approve_billing"))])
def approve(
    request: Request,
    draft_id: int,
    revision: int = Form(...),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    ym = _draft_ym(get_or_404(session, models.QBODraft, draft_id))
    try:
        qbo_billing.approve_draft(session, draft_id, revision, request.state.user)
        session.commit()
        qbo_billing.submit_draft(session, draft_id)
        session.commit()
    except (qbo_billing.BillingError, qbo_api.QboError) as exc:
        # Service persists uncertain remote outcomes before raising; do not erase them.
        session.rollback()
        return _draft_back(str(exc), draft_id, ym, back)
    return _draft_back("Submission processed. Check delivery status below.", draft_id, ym, back)


@router.post("/billing/drafts/{draft_id}/retry", dependencies=[Depends(require_perm("approve_billing"))])
def retry(draft_id: int, back: str = Form(""), session: Session = Depends(get_session)):
    ym = _draft_ym(get_or_404(session, models.QBODraft, draft_id))
    try:
        qbo_billing.submit_draft(session, draft_id)
        session.commit()
    except (qbo_billing.BillingError, qbo_api.QboError) as exc:
        session.rollback()
        return _draft_back(str(exc), draft_id, ym, back)
    return _draft_back("Submission processed. Check delivery status below.", draft_id, ym, back)


@router.post("/billing/sync", dependencies=[Depends(billing_user)])
def sync(session: Session = Depends(get_session)):
    try:
        result = qbo_billing.reconcile(session)
        session.commit()
    except (qbo_billing.BillingError, qbo_api.QboError) as exc:
        session.rollback()
        return _back(str(exc))
    return _back(
        f"Reconciled {result.get('invoices', 0)} invoices, {result.get('payments', 0)} payments, "
        f"and {result.get('credits', 0)} credits."
    )


@router.post("/billing/settings", dependencies=[Depends(require_admin)])
def settings(
    request: Request, enabled: bool = Form(False), reviewed: bool = Form(False), session: Session = Depends(get_session)
):
    if enabled and (not reviewed or not qbo_api.connected(session)):
        return _back("Connect QuickBooks and confirm billing setup before enabling invoice delivery.")
    if enabled:
        try:
            company = qbo_api.company_info(session)
            prefs = qbo_api.preferences(session)
        except qbo_api.QboError as exc:
            return _back(str(exc))
        country = str(company.get("Country", "")).upper()
        currency = (prefs.get("CurrencyPrefs") or {}).get("HomeCurrency", {}).get("value")
        if country not in ("US", "USA") or currency != "USD":
            return _back("Billing currently requires a verified United States company with USD home currency.")
        models.set_setting(session, "qbo_billing_scope", qbo_api.connection_scope(session))
    models.set_setting(session, "qbo_billing_enabled", "1" if enabled else "0")
    models.audit(session, request.state.user.id, "qbo.billing_settings", f"enabled={enabled}")
    session.commit()
    return _back("Billing settings saved.")


def sync_job(engine):
    with Session(engine) as session:
        if not qbo_api.connected(session):
            return
        if models.get_setting(session, "qbo_billing_enabled", "") == "1":
            try:
                qbo_billing.process_pending(session)
            except Exception:
                session.rollback()
                logging.getLogger(__name__).exception("QuickBooks pending submission failed")
        try:
            qbo_billing.reconcile(session)
            session.commit()
        except Exception as exc:
            session.rollback()
            models.set_setting(session, "qbo_billing_sync_error", str(exc)[:500])
            session.commit()
            raise


def mirrored_pdf(session, invoice):
    """Serve cached official PDF even while disconnected, after caller authorizes."""
    if invoice.status in ("unavailable", "deleted", "void"):
        raise HTTPException(404)
    if qbo_api.connected(session) and invoice.scope == qbo_api.connection_scope(session):
        try:
            current = qbo_api.get_invoice(session, invoice.qbo_id)
        except qbo_api.QboError:
            raise HTTPException(502, "QuickBooks invoice verification is temporarily unavailable.") from None
        customer = session.get(models.Customer, invoice.customer_id)
        if (
            not customer
            or customer.qbo_customer_scope != invoice.scope
            or str(current.get("CustomerRef", {}).get("value", "")) != customer.qbo_customer_id
            or current.get("SyncToken") != invoice.raw.get("SyncToken")
        ):
            raise HTTPException(409, "This invoice changed in QuickBooks. Sync billing before downloading it.")
    digest = hashlib.sha256(f"{invoice.scope}:{invoice.qbo_id}:{invoice.raw.get('SyncToken', '')}".encode()).hexdigest()
    path = Path(config.DATA_DIR) / "qbo-invoices" / f"{digest}.pdf"
    if not path.is_file():
        if not qbo_api.connected(session) or invoice.scope != qbo_api.connection_scope(session):
            raise HTTPException(
                409, "This PDF has not been downloaded. Reconnect the original QuickBooks company and sync."
            )
        try:
            data = qbo_api.invoice_pdf(session, invoice.qbo_id)
        except qbo_api.QboError:
            raise HTTPException(502, "QuickBooks PDF is temporarily unavailable.") from None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(str(path), data)
        session.commit()
    return Response(
        path.read_bytes(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="quickbooks-invoice-{invoice.id}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/billing/invoices/{invoice_id}/pdf", dependencies=[Depends(billing_user)])
def invoice_pdf(invoice_id: int, session: Session = Depends(get_session)):
    return mirrored_pdf(session, get_or_404(session, models.QBOInvoiceMirror, invoice_id))


@router.post("/billing/drafts/{draft_id}/approve-total", dependencies=[Depends(require_perm("approve_billing"))])
def approve_total(
    request: Request,
    draft_id: int,
    revision: int = Form(...),
    review_hash: str = Form(...),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    ym = _draft_ym(get_or_404(session, models.QBODraft, draft_id))
    try:
        qbo_billing.approve_qbo_total(session, draft_id, revision, request.state.user, review_hash=review_hash)
        session.commit()
        qbo_billing.submit_draft(session, draft_id)
        session.commit()
    except (qbo_billing.BillingError, qbo_api.QboError) as exc:
        session.rollback()
        return _draft_back(str(exc), draft_id, ym, back)
    return _draft_back("Revised QuickBooks total approved. Check delivery status.", draft_id, ym, back)


@router.get("/billing/customers/{customer_id}/link", dependencies=[Depends(require_admin)])
def link_page(request: Request, customer_id: int, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "billing/link.html", {"customer": get_or_404(session, models.Customer, customer_id), "remote": None}
    )


@router.post("/billing/customers/{customer_id}/preview-link", dependencies=[Depends(require_admin)])
def preview_link(request: Request, customer_id: int, qbo_id: str = Form(...), session: Session = Depends(get_session)):
    customer = get_or_404(session, models.Customer, customer_id)
    try:
        remote = qbo_api.get_customer(session, qbo_id.strip())
    except qbo_api.QboError as exc:
        return _back(str(exc))
    snapshot = hashlib.sha256(json.dumps(remote, sort_keys=True).encode()).hexdigest()
    request.session["qbo_link_preview"] = {
        "local_id": customer.id,
        "qbo_id": str(remote.get("Id", "")),
        "scope": qbo_api.connection_scope(session),
        "snapshot": snapshot,
    }
    return templates.TemplateResponse(request, "billing/link.html", {"customer": customer, "remote": remote})


@router.post("/billing/customers/{customer_id}/confirm-link", dependencies=[Depends(require_admin)])
def confirm_link(request: Request, customer_id: int, session: Session = Depends(get_session)):
    customer = get_or_404(session, models.Customer, customer_id)
    preview = request.session.pop("qbo_link_preview", {})
    scope = qbo_api.connection_scope(session)
    if preview.get("local_id") != customer.id or preview.get("scope") != scope or not preview.get("qbo_id"):
        raise HTTPException(409, "Preview the QuickBooks customer again before confirming.")
    try:
        remote = qbo_api.get_customer(session, preview["qbo_id"])
    except qbo_api.QboError as exc:
        return _back(str(exc))
    if hashlib.sha256(json.dumps(remote, sort_keys=True).encode()).hexdigest() != preview.get("snapshot"):
        raise HTTPException(409, "The QuickBooks customer changed. Preview the match again.")
    duplicate = session.scalar(
        select(models.Customer.id).where(
            models.Customer.id != customer.id,
            models.Customer.qbo_customer_scope == scope,
            models.Customer.qbo_customer_id == preview["qbo_id"],
        )
    )
    if duplicate:
        raise HTTPException(409, "That QuickBooks customer is already linked to another OpenPoolPro customer.")
    # Changing an established mapping can strand outstanding financial records.
    if (
        customer.qbo_customer_scope == scope
        and customer.qbo_customer_id
        and customer.qbo_customer_id != preview["qbo_id"]
    ):
        raise HTTPException(
            409, "This customer already has a verified mapping. Resolve existing billing before changing it."
        )
    customer.qbo_customer_id = preview["qbo_id"]
    customer.qbo_customer_scope = scope
    models.audit(
        session,
        request.state.user.id,
        "qbo.customer_linked",
        f"customer={customer.id} scope={scope} qbo_id={customer.qbo_customer_id}",
    )
    session.commit()
    return _back("QuickBooks customer match confirmed.")


@router.post("/billing/drafts/{draft_id}/discard", dependencies=[Depends(billing_user)])
def discard(
    request: Request,
    draft_id: int,
    revision: int = Form(...),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    # Read the month before the row can be deleted. A missing draft keeps the
    # service's own refusal (and its message) exactly as before.
    found = session.get(models.QBODraft, draft_id)
    ym = _draft_ym(found) if found else ""
    try:
        qbo_billing.discard_draft(session, draft_id, revision, request.state.user.id)
        session.commit()
    except qbo_billing.BillingError as exc:
        session.rollback()
        if ym:
            return _draft_back(str(exc), draft_id, ym, back)
        return _back(str(exc), draft_id)
    message = "Unsubmitted draft discarded. Prepare this period again to collect current work."
    if back == "list" and ym:
        return RedirectResponse(_split_url(ym, message=message), 303)
    return _back(message)

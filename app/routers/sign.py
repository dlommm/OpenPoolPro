"""Public contract e-sign flow (no session auth — see EXEMPT_PREFIX in main).

The customer gets an emailed link /sign/{token}. The token is 256 bits of
secrets.token_urlsafe entropy, single-use, and time-boxed: only its sha256
lands in contracts.sign_token_hash, and both hash and expiry are cleared the
moment the contract is signed, which also kills the token-gated PDF route.
Guessing is hopeless at that size, but an IP-keyed limiter still throttles
scanners so invalid-token noise can't probe unrated.

Signing appends a certificate page to the approved PDF (the customer signs
exactly the bytes that were emailed) and registers the result through the
same fields the staff "upload signed PDF" path uses, so the hub, digest, and
timeline see no difference between an e-signed and a hand-signed contract.
"""
import hashlib
import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import contract_html, contract_pdf, mailer, models, sign_messages, tax
from app.company import company_info
from app.contract_files import pdf_path, signed_path
from app.db import get_session
from app.fileio import atomic_write
from app.templating import templates
from app.timefmt import business_today, to_local


def _msg_ctx(contract, prop, company: dict, **extra) -> dict:
    """Token context shared by the sign-request email, signed email, and the
    confirmation page (unset extras render as empty)."""
    ctx = {
        "customer_name": prop.customer.name,
        "company_name": company.get("business_name") or "",
        "season_year": contract.season_year,
        "property_address": prop.address,
        "sign_url": "", "signer_name": "", "signed_date": "", "expiry_label": "",
    }
    ctx.update(extra)
    return ctx

router = APIRouter()

def _lookup(session: Session, token: str) -> "models.Contract | None":
    if not token or len(token) > 128:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return session.scalars(
        select(models.Contract)
        .where(models.Contract.sign_token_hash == digest,
               models.Contract.sign_token_hash != "")).first()


def _client_ip(request: Request) -> str:
    """Trusted client IP for rate-limiting — uvicorn already resolved this from
    X-Forwarded-For against FORWARDED_ALLOW_IPS, so it can't be spoofed. Keep
    security decisions on this, not the raw header."""
    return request.client.host if request.client else ""


def _signer_ip(request: Request) -> str:
    """The customer's apparent PUBLIC IP, for the signature certificate.

    Informational (not an access control), so we want the originating client
    even when the operator hasn't perfectly enumerated every internal proxy
    hop in FORWARDED_ALLOW_IPS: take the LEFTMOST X-Forwarded-For entry (the
    original client per the standard), which is the customer's public IP
    through Pangolin/Traefik. Fall back to the trusted peer when there's no
    forwarded header (direct/LAN access)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _message(request: Request, heading: str, body: str, status_code: int = 200):
    return templates.TemplateResponse(request, "sign/message.html", {
        "heading": heading, "body": body,
    }, status_code=status_code)


def _resolve(request: Request, session: Session, token: str):
    """(contract, error_response): every /sign route funnels through here so
    the invalid / expired / already-signed states render identically."""
    ip = _client_ip(request)
    limiter = request.app.state.sign_limiter
    if limiter.locked("ip:" + ip):
        return None, _message(request, "Too many attempts",
                              "Please wait a few minutes and try again.", 429)
    contract = _lookup(session, token)
    if contract is None:
        limiter.fail("ip:" + ip)
        return None, _message(request, "This link isn't valid",
                              "This signing link is not valid. It may have been replaced "
                              "by a newer email — check for a more recent message, or "
                              "contact us for a fresh link.", 404)
    if contract.status != "sent":
        return None, _message(request, "This link is no longer active",
                              "This agreement is no longer awaiting a signature. "
                              "If you believe that's wrong, please contact us.", 410)
    expires = contract.sign_token_expires_at
    if expires is None or models.utcnow() > expires:
        return None, _message(request, "This link has expired",
                              "For security, signing links only work for a limited time. "
                              "Please contact us and we'll send you a fresh one.", 410)
    return contract, None


def _page_ctx(session: Session, contract: "models.Contract", token: str) -> dict:
    prop = contract.pool.property
    company = company_info(session)
    doc_html = contract_html.render_contract_html(
        contract, contract.pool, prop, prop.customer, company,
        tax_rate_ppm=tax.resolve_rate_ppm(session, prop.state), session=session)
    return {
        "contract": contract, "pool": contract.pool, "prop": prop,
        "customer": prop.customer, "company": company,
        "token": token, "doc_html": doc_html,
        "today_local": business_today().strftime("%B %d, %Y"),
        "expires_local": to_local(contract.sign_token_expires_at),
    }


@router.get("/sign/{token}")
def sign_page(request: Request, token: str, session: Session = Depends(get_session)):
    contract, error = _resolve(request, session, token)
    if error is not None:
        return error
    # Record first-view for the audit trail (attribution evidence). One-shot,
    # committed immediately so it survives regardless of what follows.
    if contract.sign_viewed_at is None:
        contract.sign_viewed_at = models.utcnow()
        session.commit()
    return templates.TemplateResponse(request, "sign/page.html",
                                      _page_ctx(session, contract, token))


@router.get("/sign/{token}/pdf")
def sign_pdf(request: Request, token: str, session: Session = Depends(get_session)):
    contract, error = _resolve(request, session, token)
    if error is not None:
        return error
    path = pdf_path(contract)
    if not contract.pdf_filename or not os.path.isfile(path):
        return _message(request, "Document unavailable",
                        "The agreement PDF could not be loaded. Please contact us.", 404)
    # inline: the default attachment disposition forces a download, which
    # blanks the sign page's iframe and breaks "open in a new tab" — the
    # customer could never actually read what they're signing.
    return FileResponse(path, media_type="application/pdf", filename="contract.pdf",
                        content_disposition_type="inline")


@router.post("/sign/{token}")
def sign_submit(request: Request, token: str, signed_name: str = Form(""),
                agree: str = Form(""), session: Session = Depends(get_session)):
    contract, error = _resolve(request, session, token)
    if error is not None:
        return error

    name = " ".join(signed_name.split())[:120]
    if not name or not agree:
        ctx = _page_ctx(session, contract, token)
        ctx["form_error"] = ("Please type your full name and tick the agreement box "
                             "to sign.")
        ctx["signed_name"] = name
        return templates.TemplateResponse(request, "sign/page.html", ctx, status_code=422)

    if not contract.pdf_filename:
        return _message(request, "Document unavailable",
                        "The agreement PDF could not be loaded. Please contact us.", 404)

    # Claim sent->signed BEFORE the PDF work: a concurrent staff void (or a
    # double-submit of this form) loses here with a friendly page instead of
    # silently overwriting the other transition. Nothing is committed until
    # the very end, so a later failure rolls the claim back too.
    if not models.claim_contract_status(session, contract, ("sent",), "signed"):
        return _message(request, "This link is no longer active",
                        "This agreement is no longer awaiting a signature. "
                        "If you believe that's wrong, please contact us.", 410)

    now = models.utcnow()
    ip = _signer_ip(request)  # customer's public IP — audit trail only
    prop = contract.pool.property
    company = company_info(session)
    signed_date = to_local(now).strftime("%B %d, %Y")
    try:
        # Re-render the agreement with the adopted signature drawn INLINE in
        # the signature block (no separate certificate page, no IP on the
        # customer's copy) — DocuSign-style. Same frozen contract data as the
        # approved copy, so the content is identical bar the filled signature.
        signed_bytes = contract_pdf.render_contract(
            contract, contract.pool, prop, prop.customer, company,
            tax_rate_ppm=tax.resolve_rate_ppm(session, prop.state), session=session,
            signature={"name": name, "date": signed_date})
    except Exception:
        return _message(request, "Something went wrong",
                        "We couldn't record your signature just now. Please try again "
                        "in a moment, or contact us.", 500)

    atomic_write(signed_path(contract), signed_bytes)
    contract.signed_pdf_filename = "signed.pdf"
    # SHA-256 of the exact signed bytes — the tamper-evidence anchor for the
    # Certificate of Completion.
    contract.signed_pdf_hash = hashlib.sha256(signed_bytes).hexdigest()
    contract.signed_at = now
    contract.signed_name = name
    contract.signed_ip = ip
    contract.sign_token_hash = ""
    contract.sign_token_expires_at = None
    models.audit(session, None, "contract.status",
                 detail=f"{contract.id} -> signed (e-sign by {name}, ip {ip})")
    session.commit()

    # Email the customer their signed copy — strictly best-effort AFTER the
    # commit: the signature is on record either way, and a mail hiccup must
    # never show the signer an error for an agreement that IS signed.
    emailed = False
    customer = prop.customer
    mctx = _msg_ctx(contract, prop, company, signer_name=name, signed_date=signed_date)
    try:
        if customer.email and mailer.smtp_configured(session):
            mailer.send_message(
                customer.email,
                sign_messages.resolved(session, "signed_email_subject", mctx),
                sign_messages.resolved(session, "signed_email_body", mctx),
                conf=mailer.send_conf(session),
                attachments=[(signed_bytes, "signed-contract.pdf")])
            emailed = True
    except Exception:
        pass

    return templates.TemplateResponse(request, "sign/done.html", {
        "contract": contract, "customer": customer, "company": company,
        "done_heading": sign_messages.resolved(session, "sign_done_heading", mctx),
        "done_body": sign_messages.resolved(session, "sign_done_body", mctx),
        "emailed": emailed,
    })

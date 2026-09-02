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

from app import contract_pdf, models
from app.company import company_info
from app.db import get_session
from app.routers.contracts import _atomic_write, _pdf_path, _signed_path, season_value_cents
from app.templating import templates
from app.timefmt import to_local

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
    return {
        "contract": contract, "pool": contract.pool, "prop": prop,
        "customer": prop.customer, "company": company_info(session),
        "token": token, "value": season_value_cents(contract),
        "expires_local": to_local(contract.sign_token_expires_at),
    }


@router.get("/sign/{token}")
def sign_page(request: Request, token: str, session: Session = Depends(get_session)):
    contract, error = _resolve(request, session, token)
    if error is not None:
        return error
    return templates.TemplateResponse(request, "sign/page.html",
                                      _page_ctx(session, contract, token))


@router.get("/sign/{token}/pdf")
def sign_pdf(request: Request, token: str, session: Session = Depends(get_session)):
    contract, error = _resolve(request, session, token)
    if error is not None:
        return error
    path = _pdf_path(contract)
    if not contract.pdf_filename or not os.path.isfile(path):
        return _message(request, "Document unavailable",
                        "The agreement PDF could not be loaded. Please contact us.", 404)
    return FileResponse(path, media_type="application/pdf", filename="contract.pdf")


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

    path = _pdf_path(contract)
    if not contract.pdf_filename or not os.path.isfile(path):
        return _message(request, "Document unavailable",
                        "The agreement PDF could not be loaded. Please contact us.", 404)
    with open(path, "rb") as f:
        original = f.read()

    now = models.utcnow()
    ip = _client_ip(request)
    prop = contract.pool.property
    company = company_info(session)
    try:
        signed_bytes = contract_pdf.signed_contract_pdf(
            original,
            signer_name=name,
            signed_at_local=to_local(now).strftime("%B %d, %Y at %I:%M %p %Z"),
            signer_ip=ip,
            company_name=company.get("business_name") or "",
            contract_ref=(f"{prop.customer.name} — {prop.address} — "
                          f"{contract.season_year} Pool Service Agreement "
                          f"(contract #{contract.id})"))
    except Exception:
        return _message(request, "Something went wrong",
                        "We couldn't record your signature just now. Please try again "
                        "in a moment, or contact us.", 500)

    _atomic_write(_signed_path(contract), signed_bytes)
    contract.signed_pdf_filename = "signed.pdf"
    contract.status = "signed"
    contract.signed_at = now
    contract.signed_name = name
    contract.signed_ip = ip
    contract.sign_token_hash = ""
    contract.sign_token_expires_at = None
    models.audit(session, None, "contract.status",
                 detail=f"{contract.id} -> signed (e-sign by {name})")
    session.commit()

    return templates.TemplateResponse(request, "sign/done.html", {
        "contract": contract, "customer": prop.customer, "company": company,
        "signed_name": name, "signed_local": to_local(now),
    })

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models, portal
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.templating import templates

router = APIRouter()


def _manage(request, customer, session, link=None):
    photos = session.scalars(portal.photo_query(customer.id).order_by(models.Photo.id.desc()).limit(100)).all()
    shared = set(session.scalars(select(models.PortalPhoto.photo_id)).all())
    return templates.TemplateResponse(request, "portal/manage.html", {
        "customer": customer, "access": session.get(models.PortalAccess, customer.id),
        "photos": photos, "shared": shared, "link": link})


@router.get("/customers/{cid}/portal", dependencies=[Depends(require_admin)])
def manage(request: Request, cid: int, session: Session = Depends(get_session)):
    return _manage(request, get_or_404(session, models.Customer, cid), session)


@router.post("/customers/{cid}/portal/issue", dependencies=[Depends(require_admin)])
def issue(request: Request, cid: int, days: int = Form(30, ge=1, le=90),
          session: Session = Depends(get_session)):
    customer = get_or_404(session, models.Customer, cid)
    if not customer.active:
        raise HTTPException(409, "Reactivate this customer before issuing access.")
    base = portal.public_base(session)
    token = portal.issue(session, cid, days)
    models.audit(session, request.state.user.id, "portal.issued", f"customer={cid} days={days}")
    session.commit()
    return _manage(request, customer, session, f"{base}/portal/{token}")


@router.post("/customers/{cid}/portal/revoke", dependencies=[Depends(require_admin)])
def revoke(request: Request, cid: int, session: Session = Depends(get_session)):
    get_or_404(session, models.Customer, cid)
    access = session.get(models.PortalAccess, cid)
    if access:
        session.delete(access)
    models.audit(session, request.state.user.id, "portal.revoked", f"customer={cid}")
    session.commit()
    return RedirectResponse(f"/customers/{cid}/portal", 303)


@router.post("/customers/{cid}/portal/photos/{photo_id}", dependencies=[Depends(require_admin)])
def share_photo(request: Request, cid: int, photo_id: int, shared: bool = Form(False),
                session: Session = Depends(get_session)):
    photo = session.scalar(portal.photo_query(cid).where(models.Photo.id == photo_id))
    if photo is None:
        raise HTTPException(404)
    entry = session.get(models.PortalPhoto, photo_id)
    if shared and entry is None:
        session.add(models.PortalPhoto(photo_id=photo_id))
    elif not shared and entry is not None:
        session.delete(entry)
    models.audit(session, request.state.user.id, "portal.photo", f"photo={photo_id} shared={shared}")
    session.commit()
    return RedirectResponse(f"/customers/{cid}/portal", 303)


def _customer(request, token, session):
    limiter = request.app.state.sign_limiter
    key = "portal:" + (request.client.host if request.client else "unknown")
    if limiter.locked(key):
        raise HTTPException(429, "Please try again later.")
    try:
        return portal.resolve(session, token)
    except HTTPException:
        limiter.fail(key)
        raise


@router.get("/portal/{token}")
def home(request: Request, token: str, visits_page: int = Query(1, ge=1, le=10000),
         invoices_page: int = Query(1, ge=1, le=10000), session: Session = Depends(get_session)):
    customer = _customer(request, token, session)
    props = select(models.Property.id).where(models.Property.customer_id == customer.id)
    visits = session.scalars(select(models.Visit).where(models.Visit.property_id.in_(props),
        models.Visit.status == "done").order_by(models.Visit.date.desc(), models.Visit.id.desc())
        .offset((visits_page - 1) * 25).limit(25)).all()
    invoices = session.scalars(select(models.Invoice).where(models.Invoice.customer_id == customer.id,
        models.Invoice.status == "sent").order_by(models.Invoice.generated_at.desc(), models.Invoice.id.desc())
        .offset((invoices_page - 1) * 25).limit(25)).all()
    contracts = session.scalars(select(models.Contract).join(models.Pool).where(
        models.Pool.property_id.in_(props), models.Contract.status.in_(["sent", "signed"]))
        .order_by(models.Contract.season_year.desc()).limit(50)).all()
    photos = session.scalars(portal.photo_query(customer.id).join(models.PortalPhoto)
        .order_by(models.Photo.id.desc()).limit(100)).all()
    from app.routers.qbo_billing import invoice_context
    return templates.TemplateResponse(request, "portal/home.html", {
        **invoice_context(session, customer.id, sent_only=True), "qbo_portal": True,
        "customer": customer, "token": token, "visits": visits, "invoices": invoices,
        "contracts": contracts, "photos": photos, "visits_page": visits_page, "invoices_page": invoices_page,
        "company": models.get_setting(session, "business_name", "OpenPoolPro")})


@router.get("/portal/{token}/invoices/{iid}")
def invoice_file(request: Request, token: str, iid: int, session: Session = Depends(get_session)):
    customer = _customer(request, token, session)
    inv = session.scalar(select(models.Invoice).where(models.Invoice.id == iid,
        models.Invoice.customer_id == customer.id, models.Invoice.status == "sent"))
    if inv is None:
        raise HTTPException(404)
    path = portal.safe_file(f"{config.DATA_DIR}/invoices", inv.pdf_filename)
    return FileResponse(path, media_type="application/pdf", filename=f"invoice-{inv.id}.pdf")


@router.get("/portal/{token}/contracts/{cid}")
def contract_file(request: Request, token: str, cid: int, session: Session = Depends(get_session)):
    customer = _customer(request, token, session)
    contract = session.scalar(select(models.Contract).join(models.Pool).join(models.Property).where(
        models.Contract.id == cid, models.Property.customer_id == customer.id,
        models.Contract.status.in_(["sent", "signed"])))
    if contract is None:
        raise HTTPException(404)
    filename = "signed.pdf" if contract.status == "signed" else "contract.pdf"
    path = portal.safe_file(f"{config.DATA_DIR}/contracts/{cid}", filename)
    return FileResponse(path, media_type="application/pdf", filename=f"agreement-{cid}.pdf")


@router.get("/portal/{token}/photos/{photo_id}")
def photo_file(request: Request, token: str, photo_id: int, session: Session = Depends(get_session)):
    customer = _customer(request, token, session)
    photo = session.scalar(portal.photo_query(customer.id).join(models.PortalPhoto)
        .where(models.Photo.id == photo_id))
    if photo is None:
        raise HTTPException(404)
    path = portal.safe_file(f"{config.DATA_DIR}/photos/{photo.entity_type}/{photo.entity_id}", photo.filename)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/portal/{token}/quickbooks/{invoice_id}/pdf")
def quickbooks_pdf(request: Request, token: str, invoice_id: int, session: Session = Depends(get_session)):
    customer = _customer(request, token, session)
    invoice = session.scalar(select(models.QBOInvoiceMirror).where(
        models.QBOInvoiceMirror.id == invoice_id,
        models.QBOInvoiceMirror.customer_id == customer.id,
        models.QBOInvoiceMirror.email_status == "EmailSent",
        models.QBOInvoiceMirror.status.not_in(("unavailable", "deleted", "void"))))
    if invoice is None:
        raise HTTPException(404)
    from app.routers.qbo_billing import mirrored_pdf
    return mirrored_pdf(session, invoice)

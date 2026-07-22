"""Contract workflow router: draft -> approve (render PDF) -> send (email PDF)
-> signed (upload signed PDF); void is terminal from any non-void state.

Mirrors app/routers/invoices.py's patterns throughout: the PDF-download
FileResponse (404 if missing), models.audit(...) on every status change, and
atomic tmp-file + os.replace disk writes so a crash mid-write never leaves a
truncated PDF at the final path. Each contract's PDFs live under
DATA_DIR/contracts/{id}/ with fixed filenames (contract.pdf, signed.pdf), so
there is no user-controlled filename and therefore no path-traversal surface
on the two serve routes.
"""
import os
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from sqlalchemy.orm import Session

from app import config, contract_pdf, mailer, models, rates
from app.db import get_or_404, get_session
from app.deps import require_perm
from app.main import templates

router = APIRouter(dependencies=[Depends(require_perm("manage_customers"))])

# (dollar form field name, cents column name) pairs, same 6 rate fields the
# contract snapshot shares with ContractRate / rates.CONTRACT_RATE_FIELDS.
CONTRACT_DOLLAR_FIELDS = tuple(
    (cents_field.removesuffix("_cents") + "_dollars", cents_field)
    for cents_field in rates.CONTRACT_RATE_FIELDS
)


def _contract_dir(cid: int) -> str:
    d = os.path.join(config.DATA_DIR, "contracts", str(cid))
    os.makedirs(d, exist_ok=True)
    return d


def _pdf_path(contract: "models.Contract") -> str:
    return os.path.join(_contract_dir(contract.id), "contract.pdf")


def _signed_path(contract: "models.Contract") -> str:
    return os.path.join(_contract_dir(contract.id), "signed.pdf")


def _atomic_write(path: str, data: bytes) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def _parse_date_field(raw: str | None):
    """Tolerant date parse for the edit form.

    Blank -> (True, None): clears the field. Valid ISO date -> (True, date).
    Garbage -> (False, None): leave the stored value alone.
    """
    raw = (raw or "").strip()
    if not raw:
        return True, None
    try:
        return True, date.fromisoformat(raw)
    except ValueError:
        return False, None


@router.post("/pools/{pid}/contracts/new")
def new_contract(pid: int, request: Request, session: Session = Depends(get_session)):
    pool = get_or_404(session, models.Pool, pid)
    prop = pool.property
    snapshot = rates.contract_rates_for(session, pool.gallons)
    cents = snapshot or {field: 0 for field in rates.CONTRACT_RATE_FIELDS}
    includes_spa = any(p.active and p.kind == "spa" for p in prop.pools)
    contract = models.Contract(
        pool_id=pool.id, season_year=date.today().year, weeks=16,
        includes_spa=includes_spa, created_by_id=request.state.user.id,
        **cents,
    )
    session.add(contract)
    session.commit()
    return RedirectResponse(f"/contracts/{contract.id}", status_code=303)


@router.get("/contracts/{cid}")
def contract_detail(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    pool = contract.pool
    prop = pool.property
    customer = prop.customer
    return templates.TemplateResponse(request, "contracts/detail.html", {
        "contract": contract, "pool": pool, "prop": prop, "customer": customer,
        "pdf_exists": bool(contract.pdf_filename) and os.path.isfile(_pdf_path(contract)),
        "signed_exists": bool(contract.signed_pdf_filename) and os.path.isfile(_signed_path(contract)),
        "show_money": request.state.user.can("view_money"),
        "send_error": request.query_params.get("send_error"),
        "sign_error": request.query_params.get("sign_error"),
    })


@router.post("/contracts/{cid}/edit")
async def edit_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status != "draft":
        raise HTTPException(status_code=400)
    form = await request.form()

    for dollar_field, cents_field in CONTRACT_DOLLAR_FIELDS:
        raw = (form.get(dollar_field) or "").strip()
        if not raw:
            setattr(contract, cents_field, 0)
            continue
        try:
            setattr(contract, cents_field, round(float(raw) * 100))
        except ValueError:
            pass  # garbage: leave the stored value alone

    season_raw = (form.get("season_year") or "").strip()
    if season_raw:
        try:
            contract.season_year = int(season_raw)
        except ValueError:
            pass

    weeks_raw = (form.get("weeks") or "").strip()
    if weeks_raw:
        try:
            weeks_val = int(weeks_raw)
            if weeks_val >= 0:
                contract.weeks = weeks_val
        except ValueError:
            pass

    set_open, open_val = _parse_date_field(form.get("open_date"))
    if set_open:
        contract.open_date = open_val
    set_close, close_val = _parse_date_field(form.get("close_date"))
    if set_close:
        contract.close_date = close_val

    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/approve")
def approve_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    from app.routers.company import company_info

    contract = get_or_404(session, models.Contract, cid)
    if contract.status != "draft":
        raise HTTPException(status_code=400)

    pool = contract.pool
    prop = pool.property
    customer = prop.customer
    company = company_info(session)
    pdf_bytes = contract_pdf.render_contract(contract, pool, prop, customer, company)
    _atomic_write(_pdf_path(contract), pdf_bytes)

    contract.pdf_filename = "contract.pdf"
    contract.status = "approved"
    contract.approved_at = models.utcnow()
    contract.approved_by_id = request.state.user.id
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> approved")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/send")
def send_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status != "approved":
        raise HTTPException(status_code=400)

    customer = contract.pool.property.customer
    path = _pdf_path(contract)
    if not customer.email or not mailer.smtp_configured(session) or not os.path.isfile(path):
        return RedirectResponse(f"/contracts/{cid}?send_error=1", status_code=303)

    with open(path, "rb") as f:
        pdf_bytes = f.read()
    subject = f"Your {contract.season_year} Pool Service Agreement"
    body = (f"Attached is your {contract.season_year} pool service agreement. "
            "Please review and let us know if you have any questions.")
    try:
        mailer.send_message(customer.email, subject, body, conf=mailer.smtp_settings(session),
                             attachments=[(pdf_bytes, "contract.pdf")])
    except Exception:
        return RedirectResponse(f"/contracts/{cid}?send_error=1", status_code=303)

    contract.status = "sent"
    contract.sent_at = models.utcnow()
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> sent")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/mark-signed")
async def mark_signed(request: Request, cid: int, file: UploadFile,
                       session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status == "void":
        raise HTTPException(status_code=400)

    data = await file.read()
    if not data.startswith(b"%PDF"):
        return RedirectResponse(f"/contracts/{cid}?sign_error=1", status_code=303)

    _atomic_write(_signed_path(contract), data)
    contract.signed_pdf_filename = "signed.pdf"
    contract.status = "signed"
    contract.signed_at = models.utcnow()
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> signed")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/void")
def void_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status == "void":
        raise HTTPException(status_code=400)
    contract.status = "void"
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> void")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.get("/contracts/{cid}/pdf")
def contract_pdf_route(cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if not contract.pdf_filename:
        raise HTTPException(status_code=404)
    path = _pdf_path(contract)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename="contract.pdf")


@router.get("/contracts/{cid}/signed")
def contract_signed_route(cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if not contract.signed_pdf_filename:
        raise HTTPException(status_code=404)
    path = _signed_path(contract)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename="signed-contract.pdf")

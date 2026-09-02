"""Contract workflow router: draft -> approve (render PDF) -> send (email PDF)
-> signed (upload signed PDF); void is terminal from any non-void state.
Resend is allowed from "sent" (status stays sent, sent_at refreshes).

The /contracts hub is the front door: per-season register with status chips,
a needs-attention digest, and (money-gated) season totals. /contracts/new is
the pool picker and /contracts/renew rolls one season's signed contracts into
next season's drafts, repriced from the current ContractRate table. Both
literal paths must be declared before /contracts/{cid} — cid is typed int, so
"new"/"renew" would otherwise 422 instead of falling through.

Mirrors app/routers/invoices.py's patterns throughout: the PDF-download
FileResponse (404 if missing), models.audit(...) on every status change, and
atomic tmp-file + os.replace disk writes so a crash mid-write never leaves a
truncated PDF at the final path. Each contract's PDFs live under
DATA_DIR/contracts/{id}/ with fixed filenames (contract.pdf, signed.pdf), so
there is no user-controlled filename and therefore no path-traversal surface
on the two serve routes.
"""
import hashlib
import math
import os
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import contract_pdf, mailer, models, rates, tax
from app.company import company_info
from app.contract_files import pdf_path as _pdf_path
from app.contract_files import signed_path as _signed_path
from app.db import get_or_404, get_session
from app.deps import require_perm
from app.fileio import atomic_write as _atomic_write
from app.forms import cents as parse_cents
from app.forms import parse_int_tolerant, parse_page
from app.rates import season_value_cents
from app.templating import templates
from app.timefmt import business_today

router = APIRouter(dependencies=[Depends(require_perm("manage_customers"))])

STATUSES = ("draft", "approved", "sent", "signed", "void")
PAGE_SIZE = 50
# A sent contract with no signature after this many days lands in the hub's
# needs-attention digest.
STALE_SENT_DAYS = 14

# E-sign link lifetimes offered on the send form: (label, hours).
SIGN_EXPIRY_CHOICES = (
    ("24 hours", 24),
    ("3 days", 72),
    ("7 days", 168),
    ("14 days", 336),
    ("30 days", 720),
)
DEFAULT_SIGN_EXPIRY_HOURS = 168
_SIGN_EXPIRY_HOURS = {hours for _, hours in SIGN_EXPIRY_CHOICES}

# (dollar form field name, cents column name) pairs, same 6 rate fields the
# contract snapshot shares with ContractRate / rates.CONTRACT_RATE_FIELDS.
CONTRACT_DOLLAR_FIELDS = tuple(
    (cents_field.removesuffix("_cents") + "_dollars", cents_field)
    for cents_field in rates.CONTRACT_RATE_FIELDS
)


NON_VOID = ("draft", "approved", "sent", "signed")


def issue_sign_token(contract: "models.Contract", hours: int) -> str:
    """Mint a fresh e-sign token, store only its sha256, return the raw token
    for the email link. Overwrites any prior token, so a resend invalidates
    the older link. Caller commits."""
    token = secrets.token_urlsafe(32)
    contract.sign_token_hash = hashlib.sha256(token.encode()).hexdigest()
    contract.sign_token_expires_at = models.utcnow() + timedelta(hours=hours)
    return token


def _public_base_url(request: Request, session: Session) -> str:
    """Absolute origin for customer-facing links. The public_base_url Setting
    wins (for deployments where staff browse by LAN IP but customers need the
    proxied domain); otherwise the request's own origin, which honors
    X-Forwarded-* from the trusted reverse proxy."""
    configured = models.get_setting(session, "public_base_url", "").strip()
    return (configured or str(request.base_url)).rstrip("/")


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


_EAGER_CUSTOMER = selectinload(models.Contract.pool).selectinload(
    models.Pool.property).selectinload(models.Property.customer)


def _make_draft(session: Session, pool: "models.Pool", season_year: int,
                user_id: int, base: "models.Contract | None" = None) -> "models.Contract":
    """Create a draft for (pool, season): rates from the current ContractRate
    table by pool size, falling back to `base`'s prices (renewals) or zeros."""
    snapshot = rates.contract_rates_for(session, pool.gallons)
    if snapshot is None and base is not None:
        snapshot = {f: getattr(base, f) for f in rates.CONTRACT_RATE_FIELDS}
    cents = snapshot or {field: 0 for field in rates.CONTRACT_RATE_FIELDS}
    includes_spa = (base.includes_spa if base is not None
                    else any(p.active and p.kind == "spa" for p in pool.property.pools))
    contract = models.Contract(
        pool_id=pool.id, season_year=season_year,
        weeks=base.weeks if base is not None else 16,
        includes_spa=includes_spa, created_by_id=user_id, **cents)
    session.add(contract)
    return contract


def _existing_for_season(session: Session, pool_id: int, season_year: int):
    """The pool's live (non-void) contract for a season, or None."""
    return session.scalars(
        select(models.Contract)
        .where(models.Contract.pool_id == pool_id,
               models.Contract.season_year == season_year,
               models.Contract.status != "void")
        .order_by(models.Contract.id)).first()


@router.post("/pools/{pid}/contracts/new")
def new_contract(pid: int, request: Request, season_year: str = Form(""),
                 session: Session = Depends(get_session)):
    pool = get_or_404(session, models.Pool, pid)
    season = parse_int_tolerant(season_year) or business_today().year
    # Duplicate guard, mirroring the invoice guard: a pool gets one live
    # contract per season. Re-clicking "New contract" lands on the existing one.
    existing = _existing_for_season(session, pool.id, season)
    if existing is not None:
        return RedirectResponse(f"/contracts/{existing.id}", status_code=303)
    contract = _make_draft(session, pool, season, request.state.user.id)
    session.commit()
    return RedirectResponse(f"/contracts/{contract.id}", status_code=303)


@router.get("/contracts")
def contracts_page(request: Request, season: str = "", status: str = "",
                   q: str = "", page: str = "",
                   session: Session = Depends(get_session)):
    C, P, Pr, Cu = models.Contract, models.Pool, models.Property, models.Customer
    season_year = parse_int_tolerant(season) or business_today().year
    status_filter = status if status in STATUSES else None
    p = parse_page(page)
    now = models.utcnow()

    seasons = sorted({business_today().year,
                      *session.scalars(select(C.season_year).distinct())}, reverse=True)
    counts = dict(session.execute(
        select(C.status, func.count()).where(C.season_year == season_year)
        .group_by(C.status)).all())

    base = (select(C).join(P, C.pool_id == P.id).join(Pr, P.property_id == Pr.id)
            .join(Cu, Pr.customer_id == Cu.id).where(C.season_year == season_year))
    if q.strip():
        like = f"%{q.strip()}%"
        base = base.where(Cu.name.ilike(like) | Pr.address.ilike(like))
    if status_filter:
        base = base.where(C.status == status_filter)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    contracts = session.scalars(
        base.order_by(Cu.name, C.id).offset((p - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE).options(_EAGER_CUSTOMER)).all()
    rows = [{"c": c, "value": season_value_cents(c),
             "days_out": (now - c.sent_at).days if c.sent_at else None}
            for c in contracts]

    # Season-wide sweep for the digest and totals (a season is at most a few
    # hundred rows — one eager query beats four aggregate round-trips).
    season_rows = session.scalars(
        select(C).where(C.season_year == season_year).options(_EAGER_CUSTOMER)).all()
    stale_sent = sorted(
        ({"c": c, "days": (now - c.sent_at).days} for c in season_rows
         if c.status == "sent" and c.sent_at and (now - c.sent_at).days >= STALE_SENT_DAYS),
        key=lambda r: -r["days"])
    covered_pool_ids = {c.pool_id for c in season_rows if c.status != "void"}
    uncovered_q = (select(P).join(Pr, P.property_id == Pr.id)
                   .join(Cu, Pr.customer_id == Cu.id)
                   .where(P.active, Pr.active, Cu.active, P.kind == "pool")
                   .order_by(Cu.name)
                   .options(selectinload(P.property).selectinload(Pr.customer)))
    if covered_pool_ids:
        uncovered_q = uncovered_q.where(P.id.not_in(covered_pool_ids))
    uncovered = session.scalars(uncovered_q).all()

    live = [c for c in season_rows if c.status != "void"]
    totals = {
        "signed": sum(season_value_cents(c) for c in live if c.status == "signed"),
        "pipeline": sum(season_value_cents(c) for c in live if c.status != "signed"),
    }

    return templates.TemplateResponse(request, "contracts/index.html", {
        "rows": rows, "counts": counts, "statuses": STATUSES,
        "season": season_year, "seasons": seasons, "status": status_filter,
        "q": q.strip(), "page": p, "total_pages": total_pages, "total": total,
        "stale_sent": stale_sent, "stale_days": STALE_SENT_DAYS,
        "uncovered": uncovered, "totals": totals,
        "show_money": request.state.user.can("view_money"),
    })


@router.get("/contracts/new")
def new_contract_page(request: Request, q: str = "", season: str = "",
                      session: Session = Depends(get_session)):
    """Pool picker: one tap per pool creates the draft (rates prefilled by
    size), so 'new contract' is search -> click -> done."""
    C, P, Pr, Cu = models.Contract, models.Pool, models.Property, models.Customer
    season_year = parse_int_tolerant(season) or business_today().year
    pools_q = (select(P).join(Pr, P.property_id == Pr.id)
               .join(Cu, Pr.customer_id == Cu.id)
               .where(P.active, Pr.active, Cu.active)
               .order_by(Cu.name, Pr.label, P.label)
               .options(selectinload(P.property).selectinload(Pr.customer)))
    if q.strip():
        like = f"%{q.strip()}%"
        pools_q = pools_q.where(Cu.name.ilike(like) | Pr.address.ilike(like))
    pools = session.scalars(pools_q.limit(101)).all()
    truncated = len(pools) > 100
    pools = pools[:100]
    existing = {c.pool_id: c.id for c in session.scalars(
        select(C).where(C.season_year == season_year, C.status != "void"))}
    return templates.TemplateResponse(request, "contracts/new.html", {
        "pools": pools, "existing": existing, "q": q.strip(),
        "season": season_year, "seasons": [season_year - 1, season_year, season_year + 1],
        "truncated": truncated,
    })


def _renewal_plan(session: Session, from_season: int, to_season: int):
    """Signed contracts in from_season whose pool is active and has no live
    contract in to_season, each paired with what the renewal would cost at
    current ContractRate prices (carry-forward when no size tier matches)."""
    C = models.Contract
    signed = session.scalars(
        select(C).where(C.season_year == from_season, C.status == "signed")
        .options(_EAGER_CUSTOMER)).all()
    already = {c.pool_id for c in session.scalars(
        select(C).where(C.season_year == to_season, C.status != "void"))}
    plan, skipped = [], 0
    for c in signed:
        pool = c.pool
        if c.pool_id in already:
            skipped += 1
            continue
        if not (pool.active and pool.property.active and pool.property.customer.active):
            skipped += 1
            continue
        new_cents = (rates.contract_rates_for(session, pool.gallons)
                     or {f: getattr(c, f) for f in rates.CONTRACT_RATE_FIELDS})
        old_value = season_value_cents(c)
        new_value = new_cents["weekly_cents"] * c.weeks + new_cents["open_close_cents"]
        plan.append({"old": c, "pool": pool, "new_cents": new_cents,
                     "old_value": old_value, "new_value": new_value,
                     "delta": new_value - old_value})
    return plan, skipped


@router.get("/contracts/renew")
def renew_preview(request: Request, from_season: str = "", to_season: str = "",
                  session: Session = Depends(get_session)):
    f = parse_int_tolerant(from_season) or business_today().year
    t = parse_int_tolerant(to_season) or f + 1
    plan, skipped = _renewal_plan(session, f, t)
    return templates.TemplateResponse(request, "contracts/renew.html", {
        "plan": plan, "skipped": skipped, "from_season": f, "to_season": t,
        "show_money": request.state.user.can("view_money"),
    })


@router.post("/contracts/renew")
def renew_contracts(request: Request, from_season: str = Form(""),
                    to_season: str = Form(""),
                    session: Session = Depends(get_session)):
    f = parse_int_tolerant(from_season) or business_today().year
    t = parse_int_tolerant(to_season) or f + 1
    if t == f:
        raise HTTPException(status_code=400)
    plan, _ = _renewal_plan(session, f, t)
    for item in plan:
        _make_draft(session, item["pool"], t, request.state.user.id, base=item["old"])
    models.audit(session, request.state.user.id, "contract.renew",
                 detail=f"{f} -> {t}: {len(plan)} drafts")
    session.commit()
    return RedirectResponse(f"/contracts?season={t}&status=draft", status_code=303)


def _timeline(session: Session, contract: "models.Contract"):
    """Lifecycle events from the contract's own timestamps; the void moment
    comes from its audit row since void keeps no column of its own."""
    def name(uid):
        u = session.get(models.User, uid) if uid else None
        return u.name if u else ""
    events = [("Created", contract.created_at, name(contract.created_by_id))]
    if contract.approved_at:
        events.append(("Approved", contract.approved_at, name(contract.approved_by_id)))
    if contract.sent_at:
        events.append(("Sent to customer", contract.sent_at, ""))
    if contract.signed_at:
        events.append(("Signed", contract.signed_at, contract.signed_name or ""))
    if contract.status == "void":
        void_row = session.scalars(
            select(models.AuditLog)
            .where(models.AuditLog.action == "contract.status",
                   models.AuditLog.detail == f"{contract.id} -> void")
            .order_by(models.AuditLog.at.desc())).first()
        events.append(("Voided", void_row.at if void_row else None,
                       name(void_row.user_id) if void_row else ""))
    return events


@router.get("/contracts/{cid}")
def contract_detail(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    pool = contract.pool
    prop = pool.property
    customer = prop.customer
    return templates.TemplateResponse(request, "contracts/detail.html", {
        "contract": contract, "pool": pool, "prop": prop, "customer": customer,
        "timeline": _timeline(session, contract),
        "value": season_value_cents(contract),
        "pdf_exists": bool(contract.pdf_filename) and os.path.isfile(_pdf_path(contract)),
        "signed_exists": bool(contract.signed_pdf_filename) and os.path.isfile(_signed_path(contract)),
        "show_money": request.state.user.can("view_money"),
        "send_error": request.query_params.get("send_error"),
        "sign_error": request.query_params.get("sign_error"),
        "conflict": request.query_params.get("conflict"),
        "expiry_choices": SIGN_EXPIRY_CHOICES,
        "default_expiry": DEFAULT_SIGN_EXPIRY_HOURS,
        "sign_link_expires": contract.sign_token_expires_at,
        "sign_link_active": bool(
            contract.sign_token_hash and contract.sign_token_expires_at
            and models.utcnow() <= contract.sign_token_expires_at),
    })


@router.post("/contracts/{cid}/edit")
async def edit_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    # The no-op claim re-verifies draft under the write lock, so an edit
    # racing an approve can't commit rate changes onto an approved snapshot.
    if contract.status != "draft" or not models.claim_contract_status(
            session, contract, ("draft",), "draft"):
        raise HTTPException(status_code=400)
    form = await request.form()

    for dollar_field, cents_field in CONTRACT_DOLLAR_FIELDS:
        raw = (form.get(dollar_field) or "").strip()
        if not raw:
            setattr(contract, cents_field, 0)
            continue
        # forms.cents guards the "inf"/overflow 500 the scattered parser
        # copies had; None = garbage, leave the stored value alone.
        parsed = parse_cents(raw)
        if parsed is not None:
            setattr(contract, cents_field, parsed)

    season_raw = (form.get("season_year") or "").strip()
    if season_raw:
        try:
            new_season = int(season_raw)
        except ValueError:
            new_season = contract.season_year
        # Re-check the one-live-contract-per-pool-season invariant that
        # new_contract enforces: without this, editing season_year could
        # create a duplicate the hub totals and renewal flow double-count.
        if new_season != contract.season_year:
            clash = _existing_for_season(session, contract.pool_id, new_season)
            if clash is None or clash.id == contract.id:
                contract.season_year = new_season

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

    contract = get_or_404(session, models.Contract, cid)
    # Conditional claim doubles as the double-click guard: the second submit
    # sees rowcount 0 and 400s instead of re-rendering and re-auditing.
    if contract.status != "draft" or not models.claim_contract_status(
            session, contract, ("draft",), "approved"):
        raise HTTPException(status_code=400)

    pool = contract.pool
    prop = pool.property
    customer = prop.customer
    company = company_info(session)
    rate_ppm = tax.resolve_rate_ppm(session, prop.state)
    pdf_bytes = contract_pdf.render_contract(contract, pool, prop, customer, company,
                                              tax_rate_ppm=rate_ppm, session=session)
    _atomic_write(_pdf_path(contract), pdf_bytes)

    contract.pdf_filename = "contract.pdf"
    contract.approved_at = models.utcnow()
    contract.approved_by_id = request.state.user.id
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> approved")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/send")
def send_contract(request: Request, cid: int, link_expiry: str = Form(""),
                  session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    # "sent" is allowed too: resend keeps the status, refreshes sent_at, and
    # replaces the e-sign link (the previous token stops working).
    if contract.status not in ("approved", "sent"):
        raise HTTPException(status_code=400)

    customer = contract.pool.property.customer
    path = _pdf_path(contract)
    if not customer.email or not mailer.smtp_configured(session) or not os.path.isfile(path):
        return RedirectResponse(f"/contracts/{cid}?send_error=1", status_code=303)

    hours = parse_int_tolerant(link_expiry) or DEFAULT_SIGN_EXPIRY_HOURS
    if hours not in _SIGN_EXPIRY_HOURS:
        hours = DEFAULT_SIGN_EXPIRY_HOURS
    token = issue_sign_token(contract, hours)
    sign_url = f"{_public_base_url(request, session)}/sign/{token}"
    expiry_label = next(label for label, h in SIGN_EXPIRY_CHOICES if h == hours)

    with open(path, "rb") as f:
        pdf_bytes = f.read()
    subject = f"Your {contract.season_year} Pool Service Agreement"
    body = (f"Attached is your {contract.season_year} pool service agreement.\n\n"
            "You can review and sign it online here:\n\n"
            f"  {sign_url}\n\n"
            f"For security, this signing link expires in {expiry_label} and stops "
            "working once the agreement is signed. If it expires, just reply to "
            "this email and we'll send you a fresh one.\n\n"
            "Please review and let us know if you have any questions.")
    try:
        mailer.send_message(customer.email, subject, body, conf=mailer.smtp_settings(session),
                             attachments=[(pdf_bytes, "contract.pdf")])
    except Exception:
        # The freshly minted token was never committed — the session rollback
        # discards it, so a failed send leaves no live link behind.
        return RedirectResponse(f"/contracts/{cid}?send_error=1", status_code=303)

    # Claimed after the email so an SMTP failure never advances status; a
    # concurrent void between our read and here loses gracefully — the email
    # went out, but its link dies with the uncommitted token.
    if not models.claim_contract_status(session, contract,
                                        ("approved", "sent"), "sent"):
        return RedirectResponse(f"/contracts/{cid}?conflict=1", status_code=303)
    contract.sent_at = models.utcnow()
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> sent (sign link, {expiry_label})")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/mark-signed")
async def mark_signed(request: Request, cid: int, file: UploadFile,
                       session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status == "void":
        raise HTTPException(status_code=400)

    data = await file.read()
    # 25 MB cap on the signed PDF before it's written, matching the photo cap's
    # rationale — an unbounded upload otherwise pins memory + disk.
    if len(data) > 25 * 1024 * 1024:
        return RedirectResponse(f"/contracts/{cid}?sign_error=1", status_code=303)
    if not data.startswith(b"%PDF"):
        return RedirectResponse(f"/contracts/{cid}?sign_error=1", status_code=303)

    if not models.claim_contract_status(session, contract, NON_VOID, "signed"):
        return RedirectResponse(f"/contracts/{cid}?conflict=1", status_code=303)
    _atomic_write(_signed_path(contract), data)
    contract.signed_pdf_filename = "signed.pdf"
    contract.signed_at = models.utcnow()
    models.audit(session, request.state.user.id, "contract.status",
                 detail=f"{contract.id} -> signed")
    session.commit()
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@router.post("/contracts/{cid}/void")
def void_contract(request: Request, cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if contract.status == "void" or not models.claim_contract_status(
            session, contract, NON_VOID, "void"):
        raise HTTPException(status_code=400)
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
    # inline: the default attachment disposition forces a download, which
    # breaks the detail page's iframe preview and "View PDF" links.
    return FileResponse(path, media_type="application/pdf", filename="contract.pdf",
                        content_disposition_type="inline")


@router.get("/contracts/{cid}/signed")
def contract_signed_route(cid: int, session: Session = Depends(get_session)):
    contract = get_or_404(session, models.Contract, cid)
    if not contract.signed_pdf_filename:
        raise HTTPException(status_code=404)
    path = _signed_path(contract)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename="signed-contract.pdf",
                        content_disposition_type="inline")

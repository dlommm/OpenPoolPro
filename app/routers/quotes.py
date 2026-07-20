import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session, get_or_404
from app.deps import require_perm
from app.main import templates

router = APIRouter()

QUOTE_STATUSES = ("draft", "sent", "accepted", "declined", "converted")
# draft -> sent -> accepted|declined; anything else is an invalid transition (400).
QUOTE_TRANSITIONS = {"draft": {"sent"}, "sent": {"accepted", "declined"}}


def _parse_int_tolerant(s: str | None) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _cents_or_none(dollars: str | None) -> int | None:
    """Tolerant dollars->cents parse (mirrors visits.py's `_f`): blank or
    non-finite amount means "skip this row" rather than erroring out."""
    dollars = (dollars or "").strip().lstrip("$")
    if not dollars:
        return None
    try:
        f = float(dollars)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return round(f * 100)


def _apply_lines(form, quote: "models.Quote", session: Session) -> None:
    """Replace a quote's lines from submitted dynamic rows. Blank/non-finite
    amount rows are silently skipped; sort_order is the row's index among the
    submitted rows (including skipped ones, so surviving rows keep their
    original relative position)."""
    for old in list(quote.lines):
        session.delete(old)
    session.flush()
    descriptions = form.getlist("line_description")
    amounts = form.getlist("line_amount_dollars")
    for i, (desc, amt) in enumerate(zip(descriptions, amounts)):
        cents = _cents_or_none(amt)
        if cents is None:
            continue
        session.add(models.QuoteLine(quote_id=quote.id, description=(desc or "").strip(),
                                     amount_cents=cents, sort_order=i))


def _form_context(session: Session) -> dict:
    customers = session.scalars(select(models.Customer).where(models.Customer.active)
                                .order_by(models.Customer.name)).all()
    properties = session.scalars(select(models.Property).where(models.Property.active)
                                 .join(models.Customer).order_by(models.Customer.name)).all()
    job_templates = session.scalars(select(models.JobTemplate)
                                    .order_by(models.JobTemplate.title)).all()
    return {"customers": customers, "properties": properties, "job_templates": job_templates}


@router.get("/quotes")
def list_quotes(request: Request, status: str = "", session: Session = Depends(get_session),
                user=Depends(require_perm("manage_customers"))):
    stmt = select(models.Quote).order_by(models.Quote.id.desc())
    if status in QUOTE_STATUSES:  # tolerant: unknown/blank status shows everything
        stmt = stmt.where(models.Quote.status == status)
    quotes = session.scalars(stmt).all()
    totals = {q.id: sum(line.amount_cents for line in q.lines) for q in quotes}
    # Quote has no `customer` relationship (FK only) -- batch-resolve names for the register.
    customer_ids = {q.customer_id for q in quotes}
    customer_names = {c.id: c.name for c in session.scalars(
        select(models.Customer).where(models.Customer.id.in_(customer_ids))).all()} if customer_ids else {}
    return templates.TemplateResponse(request, "quotes/list.html",
        {"quotes": quotes, "status": status, "totals": totals, "customer_names": customer_names})


@router.get("/quotes/new")
def new_quote_form(request: Request, customer_id: str = "", session: Session = Depends(get_session),
                   user=Depends(require_perm("manage_customers"))):
    ctx = _form_context(session)
    ctx.update({"q": None, "selected_customer": _parse_int_tolerant(customer_id), "total_cents": 0,
               "customer": None, "prop": None})
    return templates.TemplateResponse(request, "quotes/form.html", ctx)


@router.post("/quotes/new")
async def create_quote(request: Request, session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    form = await request.form()
    customer_id = _parse_int_tolerant(form.get("customer_id"))
    if customer_id is None:
        raise HTTPException(status_code=400, detail="A customer is required")
    get_or_404(session, models.Customer, customer_id)
    q = models.Quote(customer_id=customer_id, property_id=_parse_int_tolerant(form.get("property_id")),
                     title=(form.get("title") or "").strip(), notes=form.get("notes", ""),
                     user_id=user.id)
    session.add(q)
    session.commit()
    _apply_lines(form, q, session)
    session.commit()
    return RedirectResponse(f"/quotes/{q.id}", status_code=303)


@router.get("/quotes/{qid}")
def quote_detail(request: Request, qid: int, session: Session = Depends(get_session),
                 user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    ctx = _form_context(session)
    # Quote has no `customer`/`property` relationship (FK only) -- resolve explicitly.
    ctx.update({"q": q, "selected_customer": q.customer_id,
               "total_cents": sum(line.amount_cents for line in q.lines),
               "customer": session.get(models.Customer, q.customer_id),
               "prop": session.get(models.Property, q.property_id) if q.property_id else None})
    return templates.TemplateResponse(request, "quotes/form.html", ctx)


@router.post("/quotes/{qid}/edit")
async def edit_quote(qid: int, request: Request, session: Session = Depends(get_session),
                     user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    if q.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft quotes can be edited")
    form = await request.form()
    customer_id = _parse_int_tolerant(form.get("customer_id"))
    if customer_id is not None:
        get_or_404(session, models.Customer, customer_id)
        q.customer_id = customer_id
    q.property_id = _parse_int_tolerant(form.get("property_id"))
    title = (form.get("title") or "").strip()
    if title:
        q.title = title
    q.notes = form.get("notes", "")
    session.commit()
    _apply_lines(form, q, session)
    session.commit()
    return RedirectResponse(f"/quotes/{qid}", status_code=303)


@router.post("/quotes/{qid}/status")
def transition_quote(qid: int, to: str = Form(...), session: Session = Depends(get_session),
                     user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    if to not in QUOTE_TRANSITIONS.get(q.status, set()):
        raise HTTPException(status_code=400, detail=f"Cannot move quote from {q.status} to {to}")
    q.status = to
    session.commit()
    return RedirectResponse(f"/quotes/{qid}", status_code=303)


@router.post("/quotes/{qid}/convert")
def convert_quote(qid: int, session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    if q.status != "accepted":  # also catches double-convert (status already "converted")
        raise HTTPException(status_code=400, detail="Only accepted quotes can be converted")
    property_id = q.property_id
    if property_id is None:
        first_prop = session.scalars(select(models.Property).where(
            models.Property.customer_id == q.customer_id).order_by(models.Property.id)).first()
        property_id = first_prop.id if first_prop else None
    if property_id is None:
        raise HTTPException(status_code=400,
                            detail="This customer has no property to bill the job against")
    lines = list(q.lines)
    job = models.Job(property_id=property_id, title=q.title,
                     description="; ".join(line.description for line in lines),
                     labor_cents=sum(line.amount_cents for line in lines), job_type="service")
    session.add(job)
    session.commit()
    q.status, q.job_id = "converted", job.id
    session.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/quotes/{qid}/duplicate")
def duplicate_quote(qid: int, session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    new_q = models.Quote(customer_id=q.customer_id, property_id=q.property_id,
                         title=f"{q.title} (copy)", notes=q.notes, status="draft", user_id=user.id)
    session.add(new_q)
    session.commit()
    for line in q.lines:
        session.add(models.QuoteLine(quote_id=new_q.id, description=line.description,
                                     amount_cents=line.amount_cents, sort_order=line.sort_order))
    session.commit()
    return RedirectResponse(f"/quotes/{new_q.id}", status_code=303)


@router.post("/quotes/{qid}/lines/from-template")
def insert_template_line(qid: int, template_id: int = Form(...),
                         session: Session = Depends(get_session),
                         user=Depends(require_perm("manage_customers"))):
    q = get_or_404(session, models.Quote, qid)
    if q.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft quotes can be edited")
    t = get_or_404(session, models.JobTemplate, template_id)
    session.add(models.QuoteLine(quote_id=qid, description=t.title,
                                 amount_cents=t.labor_cents, sort_order=len(q.lines)))
    session.commit()
    return RedirectResponse(f"/quotes/{qid}", status_code=303)

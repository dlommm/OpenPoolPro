from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session, get_or_404
from app.deps import require_perm
from app.main import templates
from app.rates import effective_rate_cents

router = APIRouter()


def _cents(dollars: str) -> int | None:
    dollars = (dollars or "").strip().lstrip("$")
    return None if dollars == "" else round(float(dollars) * 100)


@router.get("/customers")
def list_customers(request: Request, q: str = "", session: Session = Depends(get_session)):
    stmt = select(models.Customer).where(models.Customer.active).order_by(models.Customer.name)
    if q:
        stmt = stmt.where(models.Customer.name.ilike(f"%{q}%"))
    customers = session.scalars(stmt).all()
    return templates.TemplateResponse(request, "customers/list.html",
                                      {"customers": customers, "q": q})


@router.get("/customers/new")
def new_customer_form(request: Request, user=Depends(require_perm("manage_customers"))):
    return templates.TemplateResponse(request, "customers/form.html", {"c": None})


@router.post("/customers/new")
def create_customer(name: str = Form(...), qb_display_name: str = Form(""),
                    phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                    session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    c = models.Customer(name=name, qb_display_name=qb_display_name.strip() or name,
                        phone=phone, email=email, notes=notes)
    session.add(c)
    session.commit()
    return RedirectResponse(f"/customers/{c.id}", status_code=303)


@router.get("/customers/{cid}")
def customer_detail(request: Request, cid: int, session: Session = Depends(get_session)):
    c = get_or_404(session, models.Customer, cid)
    props = [p for p in c.properties if p.active]
    rates = {p.id: effective_rate_cents(session, p) for p in props}
    visits = session.scalars(
        select(models.Visit).join(models.Property)
        .where(models.Property.customer_id == cid, models.Visit.status != "due")
        .order_by(models.Visit.date.desc()).limit(50)).all()
    jobs = session.scalars(
        select(models.Job).join(models.Property)
        .where(models.Property.customer_id == cid, models.Job.status != "done")).all()
    return templates.TemplateResponse(request, "customers/detail.html",
        {"c": c, "props": props, "rates": rates, "visits": visits, "jobs": jobs,
         "show_money": request.state.user.can("view_money")})


@router.get("/customers/{cid}/edit")
def edit_customer_form(request: Request, cid: int, session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    return templates.TemplateResponse(request, "customers/form.html",
                                      {"c": get_or_404(session, models.Customer, cid)})


@router.post("/customers/{cid}/edit")
def edit_customer(cid: int, name: str = Form(...), qb_display_name: str = Form(""),
                  phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                  session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    c = get_or_404(session, models.Customer, cid)
    c.name, c.phone, c.email, c.notes = name, phone, email, notes
    c.qb_display_name = qb_display_name.strip() or name
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.post("/customers/{cid}/deactivate")
def deactivate_customer(cid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    get_or_404(session, models.Customer, cid).active = False
    session.commit()
    return RedirectResponse("/customers", status_code=303)


def _apply_property(p: models.Property, label, address, gate_code, dog_warning,
                    dog_note, notes, frequency, weekday, anchor_date, rate_override_dollars):
    p.label, p.address, p.gate_code, p.notes = label, address, gate_code, notes
    p.dog_warning, p.dog_note = dog_warning, dog_note
    p.frequency, p.weekday = frequency, int(weekday)
    p.anchor_date = date.fromisoformat(anchor_date) if anchor_date else None
    p.rate_override_cents = _cents(rate_override_dollars)


@router.get("/customers/{cid}/properties/new")
def new_property_form(request: Request, cid: int, session: Session = Depends(get_session),
                      user=Depends(require_perm("manage_customers"))):
    return templates.TemplateResponse(request, "customers/property_form.html",
        {"p": None, "customer": get_or_404(session, models.Customer, cid)})


@router.post("/customers/{cid}/properties/new")
def create_property(cid: int, request: Request, label: str = Form("Home"),
                    address: str = Form(""), gate_code: str = Form(""),
                    dog_warning: bool = Form(False), dog_note: str = Form(""),
                    notes: str = Form(""), frequency: str = Form("weekly"),
                    weekday: int = Form(0), anchor_date: str = Form(""),
                    rate_override_dollars: str = Form(""),
                    session: Session = Depends(get_session),
                    user=Depends(require_perm("manage_customers"))):
    p = models.Property(customer_id=cid)
    _apply_property(p, label, address, gate_code, dog_warning, dog_note, notes,
                    frequency, weekday, anchor_date, rate_override_dollars)
    session.add(p)
    session.commit()
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@router.get("/properties/{pid}/edit")
def edit_property_form(request: Request, pid: int, session: Session = Depends(get_session),
                       user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    return templates.TemplateResponse(request, "customers/property_form.html",
                                      {"p": p, "customer": p.customer})


@router.post("/properties/{pid}/edit")
def edit_property(pid: int, label: str = Form("Home"), address: str = Form(""),
                  gate_code: str = Form(""), dog_warning: bool = Form(False),
                  dog_note: str = Form(""), notes: str = Form(""),
                  frequency: str = Form("weekly"), weekday: int = Form(0),
                  anchor_date: str = Form(""), rate_override_dollars: str = Form(""),
                  session: Session = Depends(get_session),
                  user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    _apply_property(p, label, address, gate_code, dog_warning, dog_note, notes,
                    frequency, weekday, anchor_date, rate_override_dollars)
    session.commit()
    return RedirectResponse(f"/customers/{p.customer_id}", status_code=303)


@router.post("/properties/{pid}/deactivate")
def deactivate_property(pid: int, session: Session = Depends(get_session),
                        user=Depends(require_perm("manage_customers"))):
    p = get_or_404(session, models.Property, pid)
    p.active = False
    session.commit()
    return RedirectResponse(f"/customers/{p.customer_id}", status_code=303)

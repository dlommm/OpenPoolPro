import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session
from app.deps import require_admin
from app.main import templates

router = APIRouter(dependencies=[Depends(require_admin)])

EXPORT_COLUMNS = ["customer_name", "qb_display_name", "phone", "email", "customer_notes",
                  "property_label", "address", "gate_code", "dog_note", "frequency",
                  "weekday", "rate_override_dollars", "assigned_tech", "pool_shape",
                  "length_ft", "width_ft", "avg_depth_ft", "water_type"]


@router.get("/customers/export")
def export_customers(session: Session = Depends(get_session)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    props = session.scalars(select(models.Property).join(models.Customer).where(
        models.Property.active, models.Customer.active)
        .order_by(models.Customer.name)).all()
    for p in props:
        c = p.customer
        pool = next((x for x in p.pools if x.active), None)
        tech = session.get(models.User, p.assigned_tech_id) if p.assigned_tech_id else None
        w.writerow([
            c.name, c.qb_display_name, c.phone, c.email, c.notes,
            p.label, p.address, p.gate_code, p.dog_note, p.frequency, p.weekday,
            f"{p.rate_override_cents / 100:.2f}" if p.rate_override_cents is not None else "",
            tech.username if tech else "",
            pool.shape if pool else "", pool.length_ft if pool else "",
            (pool.width_ft if pool.width_ft is not None else "") if pool else "",
            pool.avg_depth_ft if pool else "", pool.water_type if pool else "",
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=customers.csv"})


import json
import os
import re
import uuid

from fastapi import UploadFile
from fastapi.responses import HTMLResponse

from app import config
from app.rates import gallons

TARGETS = ["customer_name", "qb_display_name", "phone", "email", "customer_notes",
           "property_label", "address", "gate_code", "dog_note", "frequency",
           "weekday", "rate_override_dollars", "assigned_tech", "pool_shape",
           "length_ft", "width_ft", "avg_depth_ft", "water_type"]
ALIASES = {"customer_name": {"name", "customer"}, "address": {"addr", "street"},
           "phone": {"tel", "phone_number"}, "weekday": {"day"},
           "pool_shape": {"shape"}, "length_ft": {"len", "length"},
           "width_ft": {"wid", "width"}, "avg_depth_ft": {"depth", "avg_depth"}}
MAX_BYTES = 5 * 1024 * 1024
TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _tmp_path(token: str) -> str:
    d = os.path.join(config.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"import-{token}.json")


def _load(token: str):
    if not TOKEN_RE.match(token):
        return None
    path = _tmp_path(token)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _guess(target: str, headers: list[str]) -> str:
    def norm(s):
        return s.strip().lower().replace(" ", "_")
    for h in headers:
        if norm(h) == target or norm(h) in ALIASES.get(target, set()):
            return h
    return ""


@router.get("/customers/import")
def import_form(request: Request):
    return templates.TemplateResponse(request, "csv/import.html", {"error": None})


@router.post("/customers/import")
async def import_upload(request: Request, file: UploadFile):
    data = await file.read()
    if len(data) > MAX_BYTES:
        return templates.TemplateResponse(request, "csv/import.html",
                                          {"error": "File is over 5 MB"})
    try:
        text = data.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    except (UnicodeDecodeError, csv.Error):
        return templates.TemplateResponse(request, "csv/import.html",
                                          {"error": "That file isn't readable CSV"})
    if len(rows) < 2:
        return templates.TemplateResponse(request, "csv/import.html",
                                          {"error": "No data rows found — file is empty or header-only"})
    headers, body = rows[0], rows[1:]
    token = uuid.uuid4().hex
    with open(_tmp_path(token), "w") as f:
        json.dump({"headers": headers, "rows": body}, f)
    guesses = {t: _guess(t, headers) for t in TARGETS}
    return templates.TemplateResponse(request, "csv/mapping.html", {
        "token": token, "headers": headers, "targets": TARGETS, "guesses": guesses,
        "count": len(body)})


def _mapping_from_form(form) -> dict:
    return {t: form.get(f"map_{t}", "") for t in TARGETS}


def _validate(session, payload, mapping):
    headers = payload["headers"]
    idx = {t: headers.index(h) for t, h in mapping.items() if h in headers}
    out = []
    existing = {c.name for c in session.scalars(select(models.Customer)).all()}
    seen_in_file = set()

    def val(row, t):
        i = idx.get(t)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(payload["rows"], start=2):
        name = val(row, "customer_name")
        if not name:
            out.append((n, row, "error", "customer_name is empty")); continue
        if name in existing or name in seen_in_file:
            out.append((n, row, "skip", "duplicate customer name")); continue
        wd = val(row, "weekday")
        if wd and (not wd.isdigit() or not 0 <= int(wd) <= 6):
            out.append((n, row, "error", f"weekday must be 0-6, got {wd!r}")); continue
        freq = val(row, "frequency")
        if freq and freq not in ("weekly", "biweekly", "every4weeks", "none"):
            out.append((n, row, "error", f"unknown frequency {freq!r}")); continue
        shape = val(row, "pool_shape")
        if shape:
            try:
                gallons(shape, float(val(row, "length_ft") or 0),
                        float(val(row, "width_ft") or 0) or None,
                        float(val(row, "avg_depth_ft") or 0))
            except (ValueError, TypeError):
                out.append((n, row, "error", "pool dimensions invalid")); continue
        seen_in_file.add(name)
        out.append((n, row, "ok", ""))
    return out, idx


@router.post("/customers/import/{token}/preview")
async def import_preview(request: Request, token: str,
                         session: Session = Depends(get_session)):
    payload = _load(token)
    if payload is None:
        return HTMLResponse("Import session not found — upload again", status_code=404)
    form = await request.form()
    mapping = _mapping_from_form(form)
    results, _ = _validate(session, payload, mapping)
    return templates.TemplateResponse(request, "csv/preview.html", {
        "token": token, "mapping": mapping, "results": results,
        "ok": sum(1 for r in results if r[2] == "ok"),
        "skips": sum(1 for r in results if r[2] == "skip"),
        "errors": sum(1 for r in results if r[2] == "error")})


@router.post("/customers/import/{token}/commit")
async def import_commit(request: Request, token: str,
                        session: Session = Depends(get_session)):
    payload = _load(token)
    if payload is None:
        return HTMLResponse("Import session not found — upload again", status_code=404)
    form = await request.form()
    mapping = _mapping_from_form(form)
    results, idx = _validate(session, payload, mapping)
    errors = [(n, why) for n, _, status, why in results if status == "error"]
    if errors:
        return templates.TemplateResponse(request, "csv/result.html", {
            "created": 0, "skipped": 0, "errors": errors})

    def val(row, t):
        i = idx.get(t)
        return row[i].strip() if i is not None and i < len(row) else ""

    created = skipped = 0
    for n, row, status, why in results:
        if status == "skip":
            skipped += 1
            continue
        c = models.Customer(name=val(row, "customer_name"),
                            qb_display_name=val(row, "qb_display_name") or val(row, "customer_name"),
                            phone=val(row, "phone"), email=val(row, "email"),
                            notes=val(row, "customer_notes"))
        session.add(c)
        session.flush()
        if val(row, "address") or val(row, "property_label"):
            rate = val(row, "rate_override_dollars").lstrip("$")
            try:
                rate_cents = round(float(rate) * 100) if rate else None
            except ValueError:
                rate_cents = None
            tech = None
            if val(row, "assigned_tech"):
                tech = session.scalars(select(models.User).where(
                    models.User.username == val(row, "assigned_tech").lower())).first()
            p = models.Property(
                customer_id=c.id, label=val(row, "property_label") or "Home",
                address=val(row, "address"), gate_code=val(row, "gate_code"),
                dog_note=val(row, "dog_note"), dog_warning=bool(val(row, "dog_note")),
                frequency=val(row, "frequency") or "weekly",
                weekday=int(val(row, "weekday")) if val(row, "weekday") else 0,
                rate_override_cents=rate_cents,
                assigned_tech_id=tech.id if tech else None)
            session.add(p)
            session.flush()
            shape = val(row, "pool_shape")
            if shape:
                length = float(val(row, "length_ft") or 0)
                width = float(val(row, "width_ft") or 0) or None
                depth = float(val(row, "avg_depth_ft") or 0)
                session.add(models.Pool(
                    property_id=p.id, shape=shape, length_ft=length, width_ft=width,
                    avg_depth_ft=depth, gallons=gallons(shape, length, width, depth),
                    water_type=val(row, "water_type") or "chlorine"))
        created += 1
    session.commit()
    os.remove(_tmp_path(token))
    return templates.TemplateResponse(request, "csv/result.html", {
        "created": created, "skipped": skipped, "errors": []})

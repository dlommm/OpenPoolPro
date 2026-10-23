"""Water testers: connections (admin), the water-tests inbox (office), file
import (any staff), and the visit form's "use tester readings" panel.

Every outbound call to a vendor happens on a POST or on the tech's explicit
"Find tester readings" tap -- never on an ordinary page render.
"""
import json
import os
import re
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import config, models, tester_clients, tester_import, water_readings
from app.db import get_session
from app.deps import require_admin, require_perm
from app.templating import templates

router = APIRouter()
admin = [Depends(require_admin)]
office = [Depends(require_perm("manage_customers"))]


def _properties(session: Session) -> list[models.Property]:
    return session.scalars(select(models.Property).options(selectinload(models.Property.customer))
                           .where(models.Property.active)).all()


def _property_choices(session: Session) -> list[tuple[int, str]]:
    rows = [(p.id, f"{p.customer.name} — {p.label}{(' · ' + p.street) if p.street else ''}")
            for p in _properties(session)]
    return sorted(rows, key=lambda r: r[1].lower())


def _flash(request: Request, message: str, error: bool = False) -> None:
    request.session["tester_flash"] = {"message": message[:300], "error": error}


def _get_property(session: Session, raw) -> models.Property:
    try:
        prop = session.get(models.Property, int(raw))
    except (TypeError, ValueError):
        prop = None
    if prop is None:
        raise HTTPException(400, "Choose a pool.")
    return prop


# --- connections (admin) ---------------------------------------------------------

@router.get("/testers", dependencies=admin)
def testers_page(request: Request, session: Session = Depends(get_session)):
    links = session.scalars(select(models.TesterLink).options(
        selectinload(models.TesterLink.property).selectinload(models.Property.customer))
        .order_by(models.TesterLink.source, models.TesterLink.external_label)).all()
    linked = {(lnk.source, lnk.external_key) for lnk in links}
    devices = session.scalars(select(models.TesterDevice).order_by(
        models.TesterDevice.source, models.TesterDevice.serial)).all()
    users = session.scalars(select(models.User).where(models.User.active).order_by(models.User.name)).all()
    return templates.TemplateResponse(request, "testers/settings.html", {
        "flash": request.session.pop("tester_flash", None),
        "status": tester_clients.status(session),
        "labcom_set": bool(models.get_setting(session, "labcom_token", "")),
        "ondilo_connected": bool(models.get_setting(session, "ondilo_refresh_token", "")),
        "ondilo_pools": [p for p in request.session.pop("ondilo_pools", [])
                         if ("ondilo", p["key"]) not in linked],
        "labcom_accounts": [(k, lbl) for k, lbl in tester_clients.labcom_accounts(session)
                            if ("labcom", k) not in linked],
        "links": links, "devices": devices, "users": users,
        "properties": _property_choices(session), "sources": water_readings.SOURCES,
        "public_base": models.get_setting(session, "public_base_url", "").strip()})


@router.post("/testers/labcom", dependencies=admin)
def save_labcom(request: Request, token: str = Form(""), clear: str = Form(""),
                session: Session = Depends(get_session)):
    if clear:
        models.set_setting(session, "labcom_token", "")
        models.audit(session, request.state.user.id, "tester.labcom", "token removed")
        _flash(request, "LabCOM token removed.")
    elif token.strip():
        models.set_setting(session, "labcom_token", token.strip())
        models.audit(session, request.state.user.id, "tester.labcom", "token saved")
        _flash(request, "LabCOM token saved. Use “Sync now” to check it.")
    session.commit()
    return RedirectResponse("/testers", status_code=303)


@router.post("/testers/sync", dependencies=admin)
def sync_now(request: Request, session: Session = Depends(get_session)):
    result = tester_clients.sync_all(session, force_ondilo=True)
    session.commit()
    if not result:
        _flash(request, "Nothing to sync yet — connect a tester or monitor first.", error=True)
    else:
        parts = [f"{water_readings.SOURCES[s]}: {v if isinstance(v, str) else f'{v} new test(s)'}"
                 for s, v in result.items()]
        _flash(request, " · ".join(parts), error=any(isinstance(v, str) for v in result.values()))
    return RedirectResponse("/testers", status_code=303)


def _ondilo_redirect(request: Request, session: Session) -> str:
    base = models.get_setting(session, "public_base_url", "").strip() or str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/testers/ondilo/callback"


@router.post("/testers/ondilo/connect", dependencies=admin)
def ondilo_connect(request: Request, session: Session = Depends(get_session)):
    state = secrets.token_urlsafe(24)
    request.session["ondilo_oauth_state"] = state
    return RedirectResponse(tester_clients.ondilo_authorize_url(_ondilo_redirect(request, session), state),
                            status_code=303)


@router.get("/testers/ondilo/callback", dependencies=admin)
def ondilo_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    session: Session = Depends(get_session)):
    expected = request.session.get("ondilo_oauth_state")
    if error:
        _flash(request, "Ondilo sign-in was cancelled.", error=True)
        return RedirectResponse("/testers", status_code=303)
    if not (code and state and expected and secrets.compare_digest(state, expected)):
        _flash(request, "Ondilo sign-in could not be verified — start the connection again.", error=True)
        return RedirectResponse("/testers", status_code=303)
    request.session.pop("ondilo_oauth_state", None)
    try:
        tester_clients.ondilo_complete(session, code, _ondilo_redirect(request, session))
        models.audit(session, request.state.user.id, "tester.ondilo", "connected")
        session.commit()
        _flash(request, "Ondilo connected. Load the pools shared with this login to link them.")
    except tester_clients.TesterError as e:
        _flash(request, str(e), error=True)
    return RedirectResponse("/testers", status_code=303)


@router.post("/testers/ondilo/disconnect", dependencies=admin)
def ondilo_disconnect(request: Request, session: Session = Depends(get_session)):
    models.set_setting(session, "ondilo_refresh_token", "")
    tester_clients._ondilo_cache.clear()
    models.audit(session, request.state.user.id, "tester.ondilo", "disconnected")
    session.commit()
    _flash(request, "Ondilo disconnected. Existing links are kept for when it is reconnected.")
    return RedirectResponse("/testers", status_code=303)


@router.post("/testers/ondilo/pools", dependencies=admin)
def ondilo_load_pools(request: Request, session: Session = Depends(get_session)):
    try:
        request.session["ondilo_pools"] = tester_clients.ondilo_pools(session)
        session.commit()
        if not request.session["ondilo_pools"]:
            _flash(request, "No pools are shared with this Ondilo login yet.", error=True)
    except tester_clients.TesterError as e:
        _flash(request, str(e), error=True)
    return RedirectResponse("/testers#ondilo", status_code=303)


@router.post("/testers/iopool", dependencies=admin)
def iopool_link(request: Request, api_key: str = Form(""), property_id: str = Form(""),
                pool_key: str = Form(""), session: Session = Depends(get_session)):
    """Link a homeowner's iopool pool. The key is checked against iopool
    before it is stored; an account with several pools asks which one."""
    prop = _get_property(session, property_id)
    key = api_key.strip()
    if not key:
        raise HTTPException(400, "Enter the owner's iopool API key.")
    try:
        pools = tester_clients.iopool_pools(key)
    except tester_clients.TesterError as e:
        _flash(request, str(e), error=True)
        return RedirectResponse("/testers#iopool", status_code=303)
    choices = [{"key": str(p.get("id")), "label": p.get("title") or str(p.get("id"))} for p in pools]
    chosen = next((c for c in choices if c["key"] == pool_key), None)
    if chosen is None and len(choices) == 1:
        chosen = choices[0]
    if chosen is None:
        if not choices:
            _flash(request, "That iopool account has no pools.", error=True)
            return RedirectResponse("/testers#iopool", status_code=303)
        return templates.TemplateResponse(request, "testers/iopool_choose.html", {
            "choices": choices, "api_key": key, "prop": prop})
    link = water_readings.create_link(
        session, source="iopool", external_key=chosen["key"],
        external_label=f"{chosen['label']} (iopool)", property_id=prop.id,
        user_id=request.state.user.id, secret=key)
    try:
        tester_clients.iopool_sync(session, only=[link])
    except tester_clients.TesterError:
        pass
    session.commit()
    _flash(request, f"iopool “{chosen['label']}” now reports to {prop.customer.name}.")
    return RedirectResponse("/testers#links", status_code=303)


@router.post("/testers/link", dependencies=admin)
def add_link(request: Request, source: str = Form(...), external_key: str = Form(...),
             external_label: str = Form(""), property_id: str = Form(""),
             session: Session = Depends(get_session)):
    if source not in ("labcom", "ondilo"):
        raise HTTPException(400, "Unknown source")
    prop = _get_property(session, property_id)
    water_readings.create_link(session, source=source, external_key=external_key.strip(),
                               external_label=external_label.strip(), property_id=prop.id,
                               user_id=request.state.user.id)
    session.commit()
    _flash(request, f"“{external_label}” now reports to {prop.customer.name} — {prop.label}.")
    return RedirectResponse("/testers#links", status_code=303)


@router.post("/testers/link/{link_id}/delete", dependencies=admin)
def delete_link(link_id: int, request: Request, session: Session = Depends(get_session)):
    link = session.get(models.TesterLink, link_id)
    if link is not None:
        models.audit(session, request.state.user.id, "tester.unlink",
                     f"{link.source} '{link.external_label[:60]}' from property {link.property_id}")
        session.delete(link)
        session.commit()
        _flash(request, "Link removed. Readings already tied to that pool stay where they are.")
    return RedirectResponse("/testers#links", status_code=303)


@router.post("/testers/devices/{device_id}", dependencies=admin)
def assign_device(device_id: int, request: Request, user_id: str = Form(""),
                  session: Session = Depends(get_session)):
    dev = session.get(models.TesterDevice, device_id)
    if dev is None:
        raise HTTPException(404)
    try:
        user = session.get(models.User, int(user_id)) if user_id else None
    except ValueError:
        user = None
    dev.user_id = user.id if user else None
    models.audit(session, request.state.user.id, "tester.device",
                 f"{dev.source} {dev.serial} -> {user.name if user else 'nobody'}")
    session.commit()
    return RedirectResponse("/testers#devices", status_code=303)


# --- inbox (office) ---------------------------------------------------------------

@router.get("/water-tests", dependencies=office)
def inbox(request: Request, session: Session = Depends(get_session)):
    waiting = session.scalars(select(models.DeviceReading).where(
        models.DeviceReading.property_id.is_(None), models.DeviceReading.status == "new")
        .order_by(models.DeviceReading.measured_at.desc()).limit(100)).all()
    recent = session.scalars(select(models.DeviceReading).options(
        selectinload(models.DeviceReading.property).selectinload(models.Property.customer))
        .where(models.DeviceReading.property_id.is_not(None),
               models.DeviceReading.visit_id.is_(None), models.DeviceReading.status == "new",
               models.DeviceReading.kind == "handheld")
        .order_by(models.DeviceReading.measured_at.desc()).limit(50)).all()
    owners = {d.serial: d.user.name for d in session.scalars(select(models.TesterDevice).options(
        selectinload(models.TesterDevice.user))) if d.user}
    return templates.TemplateResponse(request, "testers/inbox.html", {
        "waiting": waiting, "recent": recent, "owners": owners,
        "suggest": {r.id: water_readings.suggestions(session, r) for r in waiting},
        "properties": _property_choices(session), "sources": water_readings.SOURCES,
        "show": water_readings.display_values, "flash": request.session.pop("tester_flash", None)})


def _inbox_reading(session: Session, reading_id: int) -> models.DeviceReading:
    r = session.get(models.DeviceReading, reading_id)
    if r is None:
        raise HTTPException(404)
    if r.visit_id is not None:
        raise HTTPException(409, "This reading is already recorded on a visit. Reopen that visit to change it.")
    return r


@router.post("/water-tests/{reading_id}/assign", dependencies=office)
def assign(reading_id: int, request: Request, property_id: str = Form(""), remember: str = Form(""),
           session: Session = Depends(get_session)):
    r = _inbox_reading(session, reading_id)
    prop = _get_property(session, property_id)
    r.property_id, r.match_method, r.assigned_by_id, r.status = prop.id, "manual", request.state.user.id, "new"
    models.audit(session, request.state.user.id, "tester.assign", f"reading {r.id} -> property {prop.id}")
    if remember and r.external_key:
        water_readings.create_link(session, source=r.source, external_key=r.external_key,
                                   external_label=r.external_label, property_id=prop.id,
                                   user_id=request.state.user.id)
    session.commit()
    _flash(request, f"Reading assigned to {prop.customer.name} — {prop.label}.")
    return RedirectResponse("/water-tests", status_code=303)


@router.post("/water-tests/{reading_id}/ignore", dependencies=office)
def ignore(reading_id: int, request: Request, session: Session = Depends(get_session)):
    r = _inbox_reading(session, reading_id)
    r.status = "ignored"
    models.audit(session, request.state.user.id, "tester.ignore", f"reading {r.id}")
    session.commit()
    return RedirectResponse("/water-tests", status_code=303)


@router.post("/water-tests/{reading_id}/unassign", dependencies=office)
def unassign(reading_id: int, request: Request, session: Session = Depends(get_session)):
    r = _inbox_reading(session, reading_id)
    models.audit(session, request.state.user.id, "tester.unassign", f"reading {r.id} from property {r.property_id}")
    r.property_id, r.match_method, r.assigned_by_id = None, "", None
    session.commit()
    _flash(request, "Reading moved back to the inbox.")
    return RedirectResponse("/water-tests", status_code=303)


# --- file import (any staff) ------------------------------------------------------

def _tmp(token: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("bad token")
    d = os.path.join(config.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"watertests-{token}.json")


def _sweep_tmp() -> None:
    d = os.path.join(config.DATA_DIR, "tmp")
    if not os.path.isdir(d):
        return
    cutoff = time.time() - 86400
    for name in os.listdir(d):
        path = os.path.join(d, name)
        if name.startswith("watertests-"):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


@router.get("/water-tests/import")
def import_form(request: Request):
    return templates.TemplateResponse(request, "testers/import.html", {"error": None})


@router.post("/water-tests/import")
async def import_upload(request: Request, file: UploadFile):
    _sweep_tmp()
    try:
        rows = tester_import.read_rows(await file.read())
    except tester_import.TesterImportError as e:
        return templates.TemplateResponse(request, "testers/import.html", {"error": str(e)})
    token = uuid.uuid4().hex
    with open(_tmp(token), "w") as f:
        json.dump({"rows": rows, "user_id": request.state.user.id}, f)
    headers = rows[0]
    return templates.TemplateResponse(request, "testers/mapping.html", {
        "token": token, "headers": headers, "roles": tester_import.guess_roles(headers),
        "options": tester_import.ALL_ROLES, "preview": rows[1:6], "count": len(rows) - 1})


@router.post("/water-tests/import/{token}")
async def import_commit(token: str, request: Request, session: Session = Depends(get_session)):
    try:
        path = _tmp(token)
        with open(path) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return templates.TemplateResponse(request, "testers/import.html",
                                          {"error": "That upload expired — choose the file again."})
    if payload.get("user_id") != request.state.user.id:
        raise HTTPException(403)
    form = await request.form()
    headers, body = payload["rows"][0], payload["rows"][1:]
    valid = {r for r, _ in tester_import.ALL_ROLES}
    roles = [(form.get(f"role_{i}") or "") for i in range(len(headers))]
    roles = [r if r in valid else "" for r in roles]
    try:
        tests, skipped = tester_import.build_tests(headers, body, roles)
    except tester_import.TesterImportError as e:
        return templates.TemplateResponse(request, "testers/mapping.html", {
            "token": token, "headers": headers, "roles": roles, "options": tester_import.ALL_ROLES,
            "preview": body[:5], "count": len(body), "error": str(e)})
    created = matched = 0
    for t in tests:
        reading, made = water_readings.upsert(
            session, source="file", external_id=tester_import.external_id(t), measured_at=t["measured_at"],
            values=t["values"], device_serial=t["serial"], external_key=water_readings.label_key(t["label"]),
            external_label=t["label"], uploaded_by_id=request.state.user.id)
        created += made
        matched += made and reading.property_id is not None
    models.audit(session, request.state.user.id, "tester.import",
                 f"{created} new test(s), {len(tests) - created} already imported, {skipped} row(s) skipped")
    session.commit()
    try:
        os.remove(path)
    except OSError:
        pass
    return templates.TemplateResponse(request, "testers/imported.html", {
        "created": created, "duplicates": len(tests) - created, "skipped": skipped, "matched": matched,
        "can_inbox": request.state.user.can("manage_customers")})


# --- visit form panel -------------------------------------------------------------

def _visit(session: Session, visit_id: int) -> models.Visit:
    v = session.get(models.Visit, visit_id)
    if v is None:
        raise HTTPException(404)
    return v


@router.get("/tester-readings/visit/{visit_id}")
def visit_panel(visit_id: int, request: Request, session: Session = Depends(get_session)):
    v = _visit(session, visit_id)
    tester_clients.refresh_for_visit(session, v)
    rows = water_readings.candidates(session, v, request.state.user.id)
    return templates.TemplateResponse(request, "testers/_visit_panel.html", {
        "v": v, "rows": rows, "sources": water_readings.SOURCES, "show": water_readings.display_values,
        "warn": {r.id: water_readings.name_warning(r, v.property) for r in rows}})


@router.get("/tester-readings/visit/{visit_id}/use/{reading_id}")
def visit_use(visit_id: int, reading_id: int, request: Request, session: Session = Depends(get_session)):
    """Fill the readings grid from a chosen reading. Nothing is saved here:
    the tech still reviews the numbers and presses Mark done, and complete()
    re-checks that this reading may be used on this visit."""
    v = _visit(session, visit_id)
    allowed = {r.id: r for r in water_readings.candidates(session, v, request.state.user.id)}
    reading = allowed.get(reading_id)
    if reading is None:
        raise HTTPException(409, "That reading can't be used on this visit any more. Tap “Find tester readings” again.")
    typed = {k: request.query_params.get(k, "") for k in water_readings.VISIT_FIELDS}
    values = {k: (f"{reading.values[k]:g}" if k in (reading.values or {}) else typed[k])
              for k in water_readings.VISIT_FIELDS}
    return templates.TemplateResponse(request, "visits/_readings.html", {
        "v": v, "values": values, "reading": reading, "sources": water_readings.SOURCES,
        "extra": [(label, val) for label, val in water_readings.display_values(reading.values)
                  if label not in {water_readings.PARAM_LABELS[k] for k in water_readings.VISIT_FIELDS}]})

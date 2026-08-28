
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.auth_core import MAX_PASSWORD_LEN, hash_password
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.forms import parse_date_tolerant, parse_int_tolerant
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

FLAGS = models.User.PERMISSION_FLAGS


def _apply_flags(u: models.User, form) -> None:
    for f in FLAGS:
        setattr(u, f, form.get(f) == "on")


def _user_form_context(u: models.User | None, session: Session | None = None,
                       error: str | None = None, **extra) -> dict:
    ctx = {"u": u, "error": error, "absences": [], "cover_choices": []}
    if u is not None and session is not None:
        ctx["absences"] = session.scalars(select(models.TechAbsence).where(
            models.TechAbsence.user_id == u.id
        ).order_by(models.TechAbsence.start_date.desc())).all()
        ctx["cover_choices"] = session.scalars(select(models.User).where(
            models.User.active, models.User.id != u.id
        ).order_by(models.User.name)).all()
    ctx.update(extra)
    return ctx


def _bump_epoch(request: Request, u: models.User) -> None:
    """Invalidate every session already issued to u by bumping its epoch.
    If the acting admin is targeting their own account, refresh this
    request's own session cookie in the same response so they aren't
    logged out by their own action."""
    u.session_epoch += 1
    if u.id == request.state.user.id:
        request.session["epoch"] = u.session_epoch


@router.get("/users")
def list_users(request: Request, session: Session = Depends(get_session)):
    users = session.scalars(select(models.User).order_by(
        models.User.active.desc(), models.User.name)).all()
    return templates.TemplateResponse(request, "users/index.html", {"users": users})


@router.get("/users/new")
def new_user_form(request: Request):
    return templates.TemplateResponse(request, "users/form.html", _user_form_context(None))


@router.post("/users/new")
async def create_user(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    if not username:
        return templates.TemplateResponse(request, "users/form.html",
            _user_form_context(None, error="Username is required"))
    if not 8 <= len(password) <= MAX_PASSWORD_LEN:
        return templates.TemplateResponse(request, "users/form.html",
            _user_form_context(None,
                               error=f"Password must be 8–{MAX_PASSWORD_LEN} characters"))
    if session.scalars(select(models.User).where(models.User.username == username)).first():
        return templates.TemplateResponse(request, "users/form.html",
            _user_form_context(None, error=f'Username "{username}" is already taken'))
    u = models.User(name=form.get("name") or username, username=username,
                    role="admin" if form.get("role") == "admin" else "tech",
                    active=True, password_hash=hash_password(password))
    _apply_flags(u, form)
    session.add(u)
    session.flush()
    models.audit(session, request.state.user.id, "user.created",
                 detail=f"{u.username!r} (id={u.id}, role={u.role})")
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.get("/users/{uid}/edit")
def edit_user_form(request: Request, uid: int, session: Session = Depends(get_session)):
    u = get_or_404(session, models.User, uid)
    return templates.TemplateResponse(request, "users/form.html", _user_form_context(u, session))


@router.post("/users/{uid}/edit")
async def edit_user(request: Request, uid: int, session: Session = Depends(get_session)):
    form = await request.form()
    u = get_or_404(session, models.User, uid)
    before_role = u.role
    before_flags = {f: getattr(u, f) for f in FLAGS}
    username = (form.get("username") or "").strip().lower()
    if username and username != u.username:
        if session.scalars(select(models.User).where(models.User.username == username)).first():
            return templates.TemplateResponse(request, "users/form.html",
                _user_form_context(u, session, error=f'Username "{username}" is already taken'))
        u.username = username
    u.name = form.get("name") or u.name
    new_role = "admin" if form.get("role") == "admin" else "tech"
    if u.role == "admin" and new_role == "tech":
        if u.id == request.state.user.id:
            return templates.TemplateResponse(request, "users/form.html",
                _user_form_context(u, session, error="You can't demote yourself"))
        others = session.scalars(select(models.User).where(
            models.User.role == "admin", models.User.active,
            models.User.id != u.id)).first()
        if others is None:
            return templates.TemplateResponse(request, "users/form.html",
                _user_form_context(u, session, error="Can't demote the last active admin"))
    u.role = new_role
    _apply_flags(u, form)
    after_flags = {f: getattr(u, f) for f in FLAGS}
    if u.role != before_role or after_flags != before_flags:
        models.audit(session, request.state.user.id, "user.permissions",
                     detail=f"{u.username!r}: role {before_role}->{u.role}, flags {after_flags}")
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/password")
def reset_password(request: Request, uid: int, password: str = Form(...),
                   session: Session = Depends(get_session)):
    u = get_or_404(session, models.User, uid)
    if not 8 <= len(password) <= MAX_PASSWORD_LEN:
        return HTMLResponse(f"Password must be 8–{MAX_PASSWORD_LEN} characters",
                            status_code=400)
    u.password_hash = hash_password(password)
    _bump_epoch(request, u)
    models.audit(session, request.state.user.id, "auth.password_changed",
                 detail=f"admin reset for {u.username!r}")
    session.commit()
    return RedirectResponse("/users", status_code=303)


def _toggle_active(request, session, uid, value):
    u = get_or_404(session, models.User, uid)
    me = request.state.user
    if u.id == me.id and not value:
        return HTMLResponse("You can't disable yourself", status_code=400)
    if not value and u.role == "admin":
        others = session.scalars(select(models.User).where(
            models.User.role == "admin", models.User.active,
            models.User.id != u.id)).first()
        if others is None:
            return HTMLResponse("Can't disable the last active admin", status_code=400)
    u.active = value
    # Restoring access is as audit-worthy as revoking it — logging only the
    # disable left re-enables (including of a terminated employee) untraced.
    models.audit(session, me.id, "user.enabled" if value else "user.disabled",
                 detail=f"{u.username!r} (id={u.id})")
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/disable")
def disable_user(request: Request, uid: int, session: Session = Depends(get_session)):
    return _toggle_active(request, session, uid, False)


@router.post("/users/{uid}/enable")
def enable_user(request: Request, uid: int, session: Session = Depends(get_session)):
    return _toggle_active(request, session, uid, True)


@router.post("/users/{uid}/2fa/reset")
def reset_2fa(request: Request, uid: int, session: Session = Depends(get_session)):
    """Admin recovery path: clear a user's 2FA entirely (secret, enabled
    flag, and every backup code) -- for when they've lost their device and
    can't confirm a self-service disable. Also bumps the target's session
    epoch (mirroring logout_sessions below) so any session cookie already
    issued to them -- which was minted under the now-defunct 2FA state --
    is invalidated too; if the admin targets their own account, refresh
    this request's own session so they aren't logged out by their own
    action."""
    u = get_or_404(session, models.User, uid)
    u.totp_secret = None
    u.totp_enabled = False
    for bc in session.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == u.id)).all():
        session.delete(bc)
    _bump_epoch(request, u)
    models.audit(session, request.state.user.id, "auth.2fa_reset",
                 detail=f"{u.username!r} (id={u.id})")
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{uid}/logout-sessions")
def logout_sessions(request: Request, uid: int, session: Session = Depends(get_session)):
    """Bump the target user's session epoch, invalidating every session
    cookie already issued to them. If the admin targets their own account
    (editing themselves from this same screen), refresh this request's own
    session so they aren't logged out by their own action."""
    u = get_or_404(session, models.User, uid)
    _bump_epoch(request, u)
    models.audit(session, request.state.user.id, "auth.logout_everywhere",
                 detail=f"{u.username!r} (id={u.id})")
    session.commit()
    return RedirectResponse("/users", status_code=303)


# --- Time off (absences with cover) ------------------------------------------

@router.post("/users/{uid}/absences/new")
async def create_absence(request: Request, uid: int, session: Session = Depends(get_session)):
    form = await request.form()
    u = get_or_404(session, models.User, uid)
    start_raw = form.get("start_date") or ""
    end_raw = form.get("end_date") or ""
    cover_raw = form.get("cover_tech_id") or ""
    note = form.get("note") or ""
    start = parse_date_tolerant(start_raw)
    end = parse_date_tolerant(end_raw)
    error = None
    if start is None or end is None:
        error = "Enter valid start and end dates."
    elif end < start:
        error = "End date must be on or after the start date."
    if error:
        return templates.TemplateResponse(request, "users/form.html",
            _user_form_context(u, session, absence_error=error, absence_start=start_raw,
                               absence_end=end_raw, absence_cover=cover_raw, absence_note=note),
            status_code=400)
    cover_id = parse_int_tolerant(cover_raw)
    if cover_id is not None:
        cover_user = session.get(models.User, cover_id)
        if cover_user is None or cover_user.id == uid or not cover_user.active:
            cover_id = None  # bad/self/disabled cover -> treat as no cover rather than 500
    session.add(models.TechAbsence(user_id=uid, start_date=start, end_date=end,
                                   cover_tech_id=cover_id, note=note.strip()))
    session.commit()
    return RedirectResponse(f"/users/{uid}/edit", status_code=303)


@router.post("/users/{uid}/absences/{aid}/delete")
def delete_absence(uid: int, aid: int, session: Session = Depends(get_session)):
    a = get_or_404(session, models.TechAbsence, aid)
    if a.user_id != uid:
        raise HTTPException(status_code=404)
    session.delete(a)
    session.commit()
    return RedirectResponse(f"/users/{uid}/edit", status_code=303)

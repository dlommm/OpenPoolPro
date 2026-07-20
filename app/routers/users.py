from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.auth_core import hash_password
from app.db import get_or_404, get_session
from app.deps import require_admin
from app.main import templates

router = APIRouter(dependencies=[Depends(require_admin)])

FLAGS = models.User.PERMISSION_FLAGS


def _apply_flags(u: models.User, form) -> None:
    for f in FLAGS:
        setattr(u, f, form.get(f) == "on")


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
    return templates.TemplateResponse(request, "users/form.html", {"u": None, "error": None})


@router.post("/users/new")
async def create_user(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    if not username:
        return templates.TemplateResponse(request, "users/form.html",
            {"u": None, "error": "Username is required"})
    if len(password) < 8:
        return templates.TemplateResponse(request, "users/form.html",
            {"u": None, "error": "Password must be 8+ characters"})
    if session.scalars(select(models.User).where(models.User.username == username)).first():
        return templates.TemplateResponse(request, "users/form.html",
            {"u": None, "error": f'Username "{username}" is already taken'})
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
    return templates.TemplateResponse(request, "users/form.html", {"u": u, "error": None})


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
                {"u": u, "error": f'Username "{username}" is already taken'})
        u.username = username
    u.name = form.get("name") or u.name
    new_role = "admin" if form.get("role") == "admin" else "tech"
    if u.role == "admin" and new_role == "tech":
        if u.id == request.state.user.id:
            return templates.TemplateResponse(request, "users/form.html",
                {"u": u, "error": "You can't demote yourself"})
        others = session.scalars(select(models.User).where(
            models.User.role == "admin", models.User.active,
            models.User.id != u.id)).first()
        if others is None:
            return templates.TemplateResponse(request, "users/form.html",
                {"u": u, "error": "Can't demote the last active admin"})
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
    if len(password) < 8:
        return HTMLResponse("Password must be 8+ characters", status_code=400)
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
    if not value:
        models.audit(session, me.id, "user.disabled", detail=f"{u.username!r} (id={u.id})")
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

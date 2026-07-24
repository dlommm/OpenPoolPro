from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models, security
from app.auth_core import hash_password, verify_password
from app.db import get_session
from app.deps import current_user
from app.main import templates

router = APIRouter(dependencies=[Depends(current_user)])

ENROLL_COOKIE = "cpp_2fa_enroll"
# Same validity window as the login-time pending-2FA cookie (app/routers/auth.py).
ENROLL_MAX_AGE = 300

BANNERS = {
    "password_changed": "Password changed.",
    "2fa_disabled": "Two-factor authentication disabled.",
    "backup_codes_regenerated": "New backup codes generated. The old ones no longer work.",
    "logged_out_everywhere": "Every other signed-in session has been ended.",
}


def _bump_epoch(request: Request, user: models.User) -> None:
    """Invalidate every other session for this user, but keep the current
    request's own client logged in by handing it a fresh cookie in the
    same response (Starlette's SessionMiddleware re-signs the cookie
    whenever request.session is mutated, regardless of response type)."""
    user.session_epoch += 1
    request.session["epoch"] = user.session_epoch


def _unused_backup_codes(session: Session, user_id: int) -> list[models.BackupCode]:
    return session.scalars(select(models.BackupCode).where(
        models.BackupCode.user_id == user_id, models.BackupCode.used_at.is_(None))).all()


def _clear_backup_codes(session: Session, user_id: int) -> None:
    for bc in session.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == user_id)).all():
        session.delete(bc)


def _verify_2fa_code(session: Session, user: models.User, code: str) -> bool:
    """A valid TOTP code, or an unused backup code (which gets consumed).

    Used to confirm a sensitive 2FA change (regenerate/disable) when the
    actor is already authenticated -- mirrors the login-time check in
    app/routers/auth.py."""
    if security.totp_verify(user.totp_secret, code):
        return True
    for bc in _unused_backup_codes(session, user.id):
        if security.verify_code(code, bc.code_hash):
            bc.used_at = models.utcnow()
            return True
    return False


def _enroll_payload(request: Request, user: models.User) -> dict | None:
    """Resolve + validate the signed enroll-secret cookie for this user.

    Reuses ``security.sign_pending``/``unsign_pending`` (the same signed,
    short-lived-cookie mechanism as the login 2FA pending stage) rather
    than inventing a second signing scheme. The two pending cookies are
    distinguished by cookie NAME (``cpp_2fa`` vs ``cpp_2fa_enroll``) and by
    a ``purpose`` key inside the signed payload, so a stray/forged cookie
    of one kind can't be replayed as the other even though both share the
    same itsdangerous salt.
    """
    token = request.cookies.get(ENROLL_COOKIE)
    payload = security.unsign_pending(config.SECRET_KEY, token, max_age=ENROLL_MAX_AGE)
    if not payload or payload.get("purpose") != "2fa_enroll" or payload.get("user_id") != user.id:
        return None
    return payload


def _account_ctx(request: Request, session: Session, user: models.User, **extra) -> dict:
    ctx = {
        "user": user,
        "backup_count": len(_unused_backup_codes(session, user.id)),
        "error": None,
        "banner": None,
        "enroll": None,
        "new_backup_codes": None,
    }
    if not user.totp_enabled:
        payload = _enroll_payload(request, user)
        if payload is not None:
            secret = payload["secret"]
            ctx["enroll"] = {
                "secret": secret,
                "qr": security.qr_svg_data_uri(
                    security.provisioning_uri(secret, user.username or user.name)),
            }
    ctx.update(extra)
    return ctx


@router.get("/account")
def account_page(request: Request, banner: str | None = None,
                  session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    ctx = _account_ctx(request, session, user, banner=BANNERS.get(banner or ""))
    return templates.TemplateResponse(request, "account.html", ctx)


@router.post("/account/password")
def change_password(request: Request, current_password: str = Form(""),
                     new_password: str = Form(""), new_password_confirm: str = Form(""),
                     session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    limiter = request.app.state.account_limiter
    key = f"acct:{user.id}"
    if limiter.locked(key):
        ctx = _account_ctx(request, session, user,
                           error="Too many attempts — try again in a few minutes")
        return templates.TemplateResponse(request, "account.html", ctx)

    if not verify_password(current_password, user.password_hash):
        limiter.fail(key)
        ctx = _account_ctx(request, session, user, error="Current password is incorrect")
        return templates.TemplateResponse(request, "account.html", ctx)
    if len(new_password) < 8:
        ctx = _account_ctx(request, session, user, error="New password must be 8+ characters")
        return templates.TemplateResponse(request, "account.html", ctx)
    if new_password != new_password_confirm:
        ctx = _account_ctx(request, session, user, error="New passwords don't match")
        return templates.TemplateResponse(request, "account.html", ctx)

    user.password_hash = hash_password(new_password)
    limiter.reset(key)
    _bump_epoch(request, user)
    models.audit(session, user.id, "auth.password_changed")
    session.commit()
    return RedirectResponse("/account?banner=password_changed", status_code=303)


@router.post("/account/2fa/enable")
def start_enroll(request: Request, session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    if user.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    secret = security.new_totp_secret()
    token = security.sign_pending(config.SECRET_KEY,
                                   {"purpose": "2fa_enroll", "user_id": user.id, "secret": secret})
    resp = RedirectResponse("/account", status_code=303)
    resp.set_cookie(ENROLL_COOKIE, token, **security.cookie_flags(ENROLL_MAX_AGE))
    return resp


@router.post("/account/2fa/confirm")
def confirm_enroll(request: Request, code: str = Form(""),
                    session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    payload = _enroll_payload(request, user)
    if payload is None or user.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    secret = payload["secret"]
    if not security.totp_verify(secret, code):
        ctx = _account_ctx(request, session, user, error="Wrong code -- try again")
        return templates.TemplateResponse(request, "account.html", ctx)

    user.totp_secret = secret
    user.totp_enabled = True
    _bump_epoch(request, user)
    codes = security.make_backup_codes()
    for c in codes:
        session.add(models.BackupCode(user_id=user.id, code_hash=security.hash_code(c)))
    models.audit(session, user.id, "auth.2fa_enabled")
    session.commit()

    ctx = _account_ctx(request, session, user, new_backup_codes=codes,
                        banner="Two-factor authentication enabled.")
    resp = templates.TemplateResponse(request, "account.html", ctx)
    resp.delete_cookie(ENROLL_COOKIE, path="/")
    return resp


@router.post("/account/2fa/regenerate")
def regenerate_backup_codes(request: Request, code: str = Form(""),
                             session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    if not user.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    limiter = request.app.state.account_limiter
    key = f"acct:{user.id}"
    if limiter.locked(key):
        ctx = _account_ctx(request, session, user,
                           error="Too many attempts -- try again in a few minutes")
        return templates.TemplateResponse(request, "account.html", ctx)
    if not _verify_2fa_code(session, user, code):
        limiter.fail(key)
        ctx = _account_ctx(request, session, user, error="Wrong code -- try again")
        return templates.TemplateResponse(request, "account.html", ctx)
    limiter.reset(key)

    _clear_backup_codes(session, user.id)
    codes = security.make_backup_codes()
    for c in codes:
        session.add(models.BackupCode(user_id=user.id, code_hash=security.hash_code(c)))
    models.audit(session, user.id, "auth.backup_codes_regenerated")
    session.commit()

    ctx = _account_ctx(request, session, user, new_backup_codes=codes,
                        banner="New backup codes generated. The old ones no longer work.")
    return templates.TemplateResponse(request, "account.html", ctx)


@router.post("/account/2fa/disable")
def disable_2fa(request: Request, code: str = Form(""),
                 session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    if not user.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    limiter = request.app.state.account_limiter
    key = f"acct:{user.id}"
    if limiter.locked(key):
        ctx = _account_ctx(request, session, user,
                           error="Too many attempts -- try again in a few minutes")
        return templates.TemplateResponse(request, "account.html", ctx)
    if not _verify_2fa_code(session, user, code):
        limiter.fail(key)
        ctx = _account_ctx(request, session, user, error="Wrong code -- try again")
        return templates.TemplateResponse(request, "account.html", ctx)
    limiter.reset(key)

    user.totp_secret = None
    user.totp_enabled = False
    _clear_backup_codes(session, user.id)
    _bump_epoch(request, user)
    models.audit(session, user.id, "auth.2fa_disabled")
    session.commit()
    return RedirectResponse("/account?banner=2fa_disabled", status_code=303)


@router.post("/account/logout-everywhere")
def logout_everywhere(request: Request, session: Session = Depends(get_session)):
    user = session.get(models.User, request.state.user.id)
    _bump_epoch(request, user)
    models.audit(session, user.id, "auth.logout_everywhere")
    session.commit()
    return RedirectResponse("/account?banner=logged_out_everywhere", status_code=303)

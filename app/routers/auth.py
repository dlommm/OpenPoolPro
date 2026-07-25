import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import config, models, security
from app.auth_core import hash_password, verify_password
from app.db import get_session
from app.main import templates

# Verified against when the username is unknown, so both failure paths pay
# the same bcrypt cost (no timing-based username enumeration).
_DUMMY_HASH = hash_password("timing-equalizer")

# Locked / bad-credential / unknown-user all render this exact same page +
# message so a client can't tell the three apart.
GENERIC_LOGIN_ERROR = "Wrong username or password"
GENERIC_2FA_ERROR = "Invalid code"

PENDING_2FA_COOKIE = "cpp_2fa"
# Seconds a pending-2FA cookie stays valid for. Module-level so tests can
# monkeypatch it down (e.g. to 0) to deterministically simulate expiry
# without needing to fake real time.
PENDING_2FA_MAX_AGE = 300

# Module-level, process-lifetime rate limiter shared by the password stage
# and the 2FA stage. Namespaced single-key store: "user:"+username and
# "ip:"+client_ip. Tests must call ``limiter.reset_all()`` in setup —
# because this object is a module singleton, lock state would otherwise
# leak from one test to the next.
limiter = security.RateLimiter()

# Nonces of pending-2FA tokens that have already been spent, mapped to the
# monotonic time they stop mattering (issue time + PENDING_2FA_MAX_AGE).
# The token is a bearer credential that skips the password stage, so it must
# only work once: without this, anyone who captures the cookie (shared
# browser profile, proxy log, cookie-jar backup) can replay it with their own
# fresh TOTP code for the rest of its 5-minute life. Bounded by the same TTL
# that bounds the token itself, and pruned opportunistically on insert.
_spent_pending: dict[str, float] = {}

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _pw_fingerprint(user: models.User) -> str:
    """Short digest of the stored password hash, embedded in the pending-2FA
    token so changing the password invalidates any token already in flight."""
    return hashlib.sha256((user.password_hash or "").encode("utf-8")).hexdigest()[:16]


def _pending_nonce(request: Request) -> str | None:
    payload = security.unsign_pending(config.SECRET_KEY,
                                      request.cookies.get(PENDING_2FA_COOKIE),
                                      max_age=PENDING_2FA_MAX_AGE)
    return (payload or {}).get("nonce")


def _spend_pending(nonce: str | None) -> None:
    now = time.monotonic()
    for key, expires in list(_spent_pending.items()):
        if now >= expires:
            _spent_pending.pop(key, None)
    if nonce:
        _spent_pending[nonce] = now + PENDING_2FA_MAX_AGE


def _establish_session(request: Request, user: models.User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["epoch"] = user.session_epoch


def _pending_user(request: Request, session: Session) -> models.User | None:
    """Resolve + validate the signed cpp_2fa cookie. None if missing,
    tampered with, expired, wrong purpose, or the user is no longer
    eligible for it.

    The purpose check stops a stolen/leaked cpp_2fa_enroll token (purpose
    "2fa_enroll", see app/routers/account.py) from being replayed here as a
    cpp_2fa cookie: both cookies are signed with the same itsdangerous
    salt, so without this check a captured enroll token would resolve to a
    valid user and land straight on the 2FA code-entry form, skipping the
    password stage entirely.

    The token is also bound to the state it was minted against: a nonce that
    is burned once the token completes a login, the user's session_epoch, and
    a fingerprint of the password hash. So a captured token can't be replayed
    after use, and a password change / "log out everywhere" revokes any token
    still in flight instead of leaving a 5-minute password-free window open.
    """
    token = request.cookies.get(PENDING_2FA_COOKIE)
    payload = security.unsign_pending(config.SECRET_KEY, token, max_age=PENDING_2FA_MAX_AGE)
    if not payload or payload.get("purpose") != "login_2fa":
        return None
    nonce = payload.get("nonce")
    if not nonce or nonce in _spent_pending:
        return None
    user = session.get(models.User, payload.get("user_id"))
    if user is None or not user.active or not user.totp_enabled:
        return None
    if payload.get("epoch") != user.session_epoch:
        return None
    if payload.get("pw") != _pw_fingerprint(user):
        return None
    return user


def _clear_pending_redirect(target: str, status_code: int = 303) -> RedirectResponse:
    resp = RedirectResponse(target, status_code=status_code)
    resp.delete_cookie(PENDING_2FA_COOKIE, path="/")
    return resp


def _record_failure(session: Session, user_id: int | None, user_key: str, ip_key: str,
                     detail: str) -> None:
    """Shared fail/audit path for both the password stage and the 2FA stage.

    Fails both rate-limit keys, audits ``auth.fail`` with ``detail``, then
    audits ``auth.lockout`` once per key that is locked *after* this call.
    Callers only reach here once they've already confirmed neither key was
    locked on entry, so a ``locked()`` reading True here can only mean this
    failure just crossed the threshold -- "first lock only" semantics for
    free, no extra bookkeeping needed.
    """
    limiter.fail(user_key)
    limiter.fail(ip_key)
    models.audit(session, user_id, "auth.fail", detail=detail)
    session.commit()
    if limiter.locked(user_key):
        models.audit(session, user_id, "auth.lockout", detail=user_key)
        session.commit()
    if limiter.locked(ip_key):
        models.audit(session, user_id, "auth.lockout", detail=ip_key)
        session.commit()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""),
          session: Session = Depends(get_session)):
    uname = username.strip().lower()
    ip = _client_ip(request)
    user_key, ip_key = "user:" + uname, "ip:" + ip

    # Rate-limit check BEFORE password verify, for both keys.
    if limiter.locked(user_key) or limiter.locked(ip_key):
        return templates.TemplateResponse(request, "login.html", {"error": GENERIC_LOGIN_ERROR})

    user = session.scalars(select(models.User).where(
        models.User.username == uname,
        models.User.active)).first()
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not ok:
        _record_failure(session, user.id if user else None, user_key, ip_key,
                         detail=f"login for {uname!r}")
        return templates.TemplateResponse(request, "login.html", {"error": GENERIC_LOGIN_ERROR})

    if user.totp_enabled:
        token = security.sign_pending(config.SECRET_KEY,
                                       {"user_id": user.id, "purpose": "login_2fa",
                                        "nonce": secrets.token_hex(16),
                                        "epoch": user.session_epoch,
                                        "pw": _pw_fingerprint(user)})
        resp = RedirectResponse("/login/2fa", status_code=303)
        resp.set_cookie(PENDING_2FA_COOKIE, token, **security.cookie_flags(PENDING_2FA_MAX_AGE))
        return resp

    limiter.reset(user_key)
    limiter.reset(ip_key)
    _establish_session(request, user)
    models.audit(session, user.id, "auth.login", detail="password")
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/login/2fa")
def login_2fa_page(request: Request, session: Session = Depends(get_session)):
    if _pending_user(request, session) is None:
        return _clear_pending_redirect("/login")
    return templates.TemplateResponse(request, "login_2fa.html", {"error": None})


@router.post("/login/2fa")
def login_2fa(request: Request, code: str = Form(""), session: Session = Depends(get_session)):
    user = _pending_user(request, session)
    if user is None:
        return _clear_pending_redirect("/login")

    ip = _client_ip(request)
    user_key, ip_key = "user:" + user.username, "ip:" + ip

    if limiter.locked(user_key) or limiter.locked(ip_key):
        return templates.TemplateResponse(request, "login_2fa.html", {"error": GENERIC_2FA_ERROR})

    ok = security.totp_verify(user.totp_secret, code)
    used_backup_code = None
    if not ok:
        unused = session.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == user.id,
            models.BackupCode.used_at.is_(None))).all()
        for bc in unused:
            if security.verify_code(code, bc.code_hash):
                ok = True
                used_backup_code = bc
                break

    if not ok:
        _record_failure(session, user.id, user_key, ip_key, detail="2fa")
        return templates.TemplateResponse(request, "login_2fa.html", {"error": GENERIC_2FA_ERROR})

    backup_remaining = None
    if used_backup_code is not None:
        # Claim the code with a conditional UPDATE rather than a plain assign:
        # the read above and this write are not atomic, so two concurrent
        # submissions of the same code could otherwise both see it unused and
        # both mint a session. rowcount == 0 means another request won the
        # race and already spent it -- treat that as a failed code.
        claimed = session.execute(
            update(models.BackupCode)
            .where(models.BackupCode.id == used_backup_code.id,
                   models.BackupCode.used_at.is_(None))
            .values(used_at=models.utcnow()))
        if claimed.rowcount == 0:
            session.rollback()
            _record_failure(session, user.id, user_key, ip_key, detail="2fa")
            return templates.TemplateResponse(request, "login_2fa.html",
                                              {"error": GENERIC_2FA_ERROR})
        backup_remaining = len(session.scalars(select(models.BackupCode).where(
            models.BackupCode.user_id == user.id,
            models.BackupCode.used_at.is_(None))).all())
        models.audit(session, user.id, "auth.backup_code_used",
                     detail=f"{backup_remaining} remaining")

    limiter.reset(user_key)
    limiter.reset(ip_key)
    _spend_pending(_pending_nonce(request))
    _establish_session(request, user)
    models.audit(session, user.id, "auth.login", detail="password+2fa")
    session.commit()

    target = "/" if backup_remaining is None else f"/?backup_remaining={backup_remaining}"
    resp = RedirectResponse(target, status_code=303)
    resp.delete_cookie(PENDING_2FA_COOKIE, path="/")
    return resp


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    """Log out server-side, not just client-side.

    The session cookie is a stateless signed token with a 90-day lifetime, so
    clearing the browser's copy leaves any cookie captured beforehand valid
    for the rest of that window. Bumping session_epoch is what actually
    revokes it (the auth middleware compares the cookie's epoch against the
    user's) -- every other credential event already does this; logout was the
    one omission.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        db_user = session.get(models.User, user.id)
        if db_user is not None:
            db_user.session_epoch += 1
            models.audit(session, db_user.id, "auth.logout")
            session.commit()
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(PENDING_2FA_COOKIE, path="/")
    return resp

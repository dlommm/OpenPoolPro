"""First-run setup: state, the setup code, owner-reset tokens, and the
Getting started checklist.

There is no default owner account and no password in the environment. Until
an active admin with a password exists, every page sends the visitor to
/setup, and starting setup needs a one-time code printed in the server log --
so whoever finds a fresh install on the network can't claim it first.

`setup_status` setting:
  missing        a fresh install with no owner yet
  "in_progress"  the owner exists and is working through the optional steps
  "legacy"       upgraded from the APP_PASSWORD era: the owner must finish
                 their profile (real name, username, new password) once
  "done"         finished, or every remaining step skipped
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select

from app import models

log = logging.getLogger("app.setup")

# (key, label, sub) -- the order of the wizard. "account" is the only
# required step; everything after it can be skipped and changed in Admin.
STEPS = (
    ("welcome", "Welcome", "What we’ll do"),
    ("account", "Your account", "Required"),
    ("security", "Two-step sign-in", "Strongly recommended"),
    ("business", "Business", "Name, contact, logo"),
    ("prices", "Prices", "Rates and labour"),
    ("water", "Water", "Targets, chemicals"),
    ("look", "Look", "Colours and layout"),
    ("crew", "Crew", "Optional"),
    ("connect", "Connections", "Email and more"),
    ("review", "Review", "Check and finish"),
)
KEYS = tuple(k for k, *_ in STEPS)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
RESET_TTL = timedelta(minutes=30)


def owner_exists(session) -> bool:
    return session.scalars(select(models.User.id).where(
        models.User.role == "admin", models.User.active,
        models.User.password_hash.is_not(None))).first() is not None


def status(session) -> str:
    return models.get_setting(session, "setup_status", "") or ""


def step_index(session) -> int:
    """Where an in-progress owner resumes (index into STEPS)."""
    try:
        i = int(models.get_setting(session, "setup_step", "2"))
    except (TypeError, ValueError):
        i = 2
    return max(2, min(i, len(STEPS) - 1))


def skipped(session) -> set[str]:
    raw = models.get_setting(session, "setup_skipped", "") or ""
    return {k for k in raw.split(",") if k in KEYS}


def new_setup_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def code_matches(expected: str | None, presented: str) -> bool:
    if not expected:
        return False
    norm = "".join(ch for ch in (presented or "").upper() if ch.isalnum())
    return secrets.compare_digest(norm, expected.replace("-", ""))


def announce_setup_code(code: str) -> None:
    """Printed where `docker compose logs` shows it. Logged at WARNING so the
    default log level never hides it."""
    log.warning("No owner account yet. Open the app in a browser to set it up. Setup code: %s", code)


def username_ok(u: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,31}", u or ""))


def suggest_username(name: str) -> str:
    import re
    import unicodedata
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9 ]", "", plain).split()
    if not words:
        return ""
    return (words[0] + "." + words[-1] if len(words) > 1 else words[0])[:32]


# ---------- owner reset (python -m app.manage reset-owner) ----------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset(session, user_id: int, clear_2fa: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    expires = models.utcnow() + RESET_TTL
    models.set_setting(session, "owner_reset",
                       f"{_hash(token)}|{user_id}|{expires.isoformat()}|{int(clear_2fa)}")
    return token


def redeem_reset(session, token: str, consume: bool = False):
    """(user, clear_2fa) for a live token, else (None, False)."""
    from datetime import datetime
    raw = models.get_setting(session, "owner_reset", "") or ""
    try:
        h, uid, exp, clear = raw.split("|")
        expires = datetime.fromisoformat(exp)
    except ValueError:
        return None, False
    if not token or not secrets.compare_digest(h, _hash(token)) or models.utcnow() > expires:
        return None, False
    user = session.get(models.User, int(uid))
    if user is None or not user.active or user.role != "admin":
        return None, False
    if consume:
        models.set_setting(session, "owner_reset", "")
    return user, clear == "1"


# ---------- Getting started (dashboard) ----------

def checklist(session, user) -> list[dict]:
    from app import mailer, qbo_api
    has_customers = session.scalars(select(models.Customer.id)).first() is not None
    biz = bool((models.get_setting(session, "business_name", "") or "").strip())
    try:
        qbo = qbo_api.connected(session)
    except Exception:  # a broken QuickBooks token must not break the dashboard
        qbo = False
    return [
        {"label": "Create your owner account", "done": True},
        {"label": "Business details and logo", "done": biz, "href": "/settings#business", "cta": "Add"},
        {"label": "Add your first customer, or import a spreadsheet", "done": has_customers,
         "href": "/customers/new", "cta": "Add customer"},
        {"label": "Set up email so visit reports reach customers",
         "done": mailer.smtp_configured(session), "href": "/settings#system", "cta": "Set up"},
        {"label": "Connect QuickBooks for monthly billing", "done": qbo,
         "href": "/billing", "cta": "Connect"},
        {"label": "Turn on two-step sign-in for your account", "done": bool(user and user.totp_enabled),
         "href": "/account", "cta": "Turn on", "warn": True},
    ]


def show_checklist(session) -> bool:
    return (models.get_setting(session, "getting_started_hidden", "") != "1"
            and status(session) in ("in_progress", "done"))

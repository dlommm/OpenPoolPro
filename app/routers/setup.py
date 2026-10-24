"""First-run setup wizard, the upgraded owner's profile step, and the
owner-reset page (see app/setup_flow.py for the rules).

Before an owner exists these routes are public (main.py lets /setup through
and sends everything else here) but every one of them needs the setup code
proven in this browser session. Afterwards they are admin-only, like Admin.
"""
import math
import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chem_targets, config, mailer, models, palettes, security, setup_flow, templating
from app.auth_core import MAX_PASSWORD_LEN, hash_password, verify_password
from app.company import store_logo
from app.db import get_session
from app.templating import templates

router = APIRouter()

MIN_PASSWORD = 12
# Turning on 2FA right after creating the account doesn't ask for the password
# again, but only for this long after the account was made in this browser.
FRESH_SECONDS = 30 * 60
TIMEZONES = (("America/New_York", "Eastern"), ("America/Chicago", "Central"),
             ("America/Denver", "Mountain"), ("America/Phoenix", "Arizona"),
             ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"),
             ("Pacific/Honolulu", "Hawaii"))
WORDS = ("tide", "reef", "kelp", "cove", "surf", "gull", "palm", "sand", "dune", "shoal",
         "brine", "coral", "marlin", "pier", "skiff", "wave")


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _owner_gate(request: Request):
    """None when this request may run an owner-only setup step, else a redirect."""
    user = request.state.user
    if request.app.state.needs_owner:
        return _go("/setup")
    if user is None or user.role != "admin":
        return _go("/")
    return None


def _ctx(request: Request, session: Session, key: str, **extra) -> dict:
    i = setup_flow.KEYS.index(key)
    seen = setup_flow.step_index(session) if not request.app.state.needs_owner else 1
    seen = max(seen, i)
    steps = [{"key": k, "label": label, "sub": sub, "n": n + 1, "on": k == key,
              "done": n < seen and k != key, "open": n <= seen and (n >= 2 or request.app.state.needs_owner)}
             for n, (k, label, sub) in enumerate(setup_flow.STEPS)]
    return {"key": key, "steps": steps, "n": i + 1, "total": len(setup_flow.STEPS),
            "label": setup_flow.STEPS[i][1], "skipped": setup_flow.skipped(session),
            "biz": models.get_setting(session, "business_name", "") or "", "error": None, **extra}


def _advance(session: Session, key: str, skip: bool = False) -> RedirectResponse:
    i = setup_flow.KEYS.index(key)
    sk = setup_flow.skipped(session)
    if skip:
        sk.add(key)
    else:
        sk.discard(key)
    models.set_setting(session, "setup_skipped", ",".join(sorted(sk)))
    nxt = min(i + 1, len(setup_flow.STEPS) - 1)
    models.set_setting(session, "setup_step", str(max(nxt, setup_flow.step_index(session))))
    session.commit()
    return _go(f"/setup/{setup_flow.KEYS[nxt]}")


# ---------- entry and the setup code ----------

@router.get("/setup")
def setup_home(request: Request, session: Session = Depends(get_session)):
    if request.app.state.needs_owner:
        if request.session.get("setup_ok"):
            return _go("/setup/welcome")
        return templates.TemplateResponse(request, "setup/gate.html", {"error": None})
    gate = _owner_gate(request)
    if gate:
        return gate
    st = setup_flow.status(session)
    if st == "legacy":
        return _go("/setup/profile")
    if st == "in_progress":
        return _go(f"/setup/{setup_flow.KEYS[setup_flow.step_index(session)]}")
    return _go("/dashboard")


@router.post("/setup/code")
async def setup_code(request: Request):
    if not request.app.state.needs_owner:
        return _go("/setup")
    limiter = request.app.state.account_limiter
    key = "setup:" + (request.client.host if request.client else "?")
    if limiter.locked(key):
        return templates.TemplateResponse(request, "setup/gate.html",
                                          {"error": "Too many tries. Wait a few minutes and try again."})
    form = await request.form()
    if not setup_flow.code_matches(request.app.state.setup_code, form.get("code") or ""):
        limiter.fail(key)
        return templates.TemplateResponse(request, "setup/gate.html",
                                          {"error": "That code doesn't match the one in the server log."})
    limiter.reset(key)
    request.session["setup_ok"] = True
    return _go("/setup/welcome")


# ---------- steps before the owner exists ----------

def _pre_owner(request: Request):
    if not request.app.state.needs_owner:
        return _go("/setup")
    if not request.session.get("setup_ok"):
        return _go("/setup")
    return None


@router.get("/setup/welcome")
def welcome(request: Request, session: Session = Depends(get_session)):
    gate = _pre_owner(request)
    if gate:
        return gate
    return templates.TemplateResponse(request, "setup/step.html", _ctx(request, session, "welcome"))


@router.get("/setup/account")
def account_form(request: Request, session: Session = Depends(get_session)):
    gate = _pre_owner(request)
    if gate:
        return gate
    return templates.TemplateResponse(request, "setup/step.html",
                                      _ctx(request, session, "account", form={}))


@router.post("/setup/account")
async def create_owner(request: Request, session: Session = Depends(get_session)):
    gate = _pre_owner(request)
    if gate:
        return gate
    form = await request.form()
    name = (form.get("name") or "").strip()[:80]
    username = (form.get("username") or "").strip().lower() or setup_flow.suggest_username(name)
    email = (form.get("email") or "").strip()[:200]
    pw, pw2 = form.get("password") or "", form.get("password_confirm") or ""
    error = None
    if len(name) < 2:
        error = "Enter your full name."
    elif not setup_flow.username_ok(username):
        error = "Use 3–32 lowercase letters, numbers, dots or dashes for the username."
    elif session.scalars(select(models.User).where(models.User.username == username)).first():
        error = "That username is taken."
    elif not MIN_PASSWORD <= len(pw) <= MAX_PASSWORD_LEN:
        error = f"Use a password of {MIN_PASSWORD} to {MAX_PASSWORD_LEN} characters."
    elif pw != pw2:
        error = "The two passwords don't match."
    if error:
        return templates.TemplateResponse(request, "setup/step.html", _ctx(
            request, session, "account", error=error,
            form={"name": name, "username": username, "email": email}), status_code=400)
    user = models.User(name=name, username=username, role="admin", active=True,
                       password_hash=hash_password(pw))
    session.add(user)
    if email:
        models.set_setting(session, "owner_email", email)
    models.set_setting(session, "setup_status", "in_progress")
    models.set_setting(session, "setup_step", "2")
    session.flush()
    models.audit(session, user.id, "setup.owner_created", detail=username)
    session.commit()
    request.app.state.needs_owner = False
    request.app.state.setup_code = None
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["epoch"] = user.session_epoch
    request.session["setup_fresh"] = int(time.time())
    return _go("/setup/security")


# ---------- owner steps ----------

def _fresh(request: Request) -> bool:
    return time.time() - (request.session.get("setup_fresh") or 0) < FRESH_SECONDS


@router.get("/setup/security")
def security_step(request: Request, session: Session = Depends(get_session)):
    gate = _owner_gate(request)
    if gate:
        return gate
    from app.routers.account import _enroll_payload
    user = session.get(models.User, request.state.user.id)
    enroll = None
    if not user.totp_enabled:
        payload = _enroll_payload(request, user)
        if payload:
            enroll = {"secret": payload["secret"], "qr": security.qr_svg_data_uri(
                security.provisioning_uri(payload["secret"], user.username or user.name))}
    return templates.TemplateResponse(request, "setup/step.html", _ctx(
        request, session, "security", user=user, enroll=enroll, fresh=_fresh(request),
        confirm_skip=request.query_params.get("confirm_skip") == "1"))


@router.post("/setup/security/start")
async def security_start(request: Request, session: Session = Depends(get_session)):
    gate = _owner_gate(request)
    if gate:
        return gate
    from app.routers.account import ENROLL_COOKIE, ENROLL_MAX_AGE
    user = session.get(models.User, request.state.user.id)
    if user.totp_enabled:
        return _go("/setup/security")
    if not _fresh(request):
        # Same step-up as Account: binding an authenticator needs the password.
        form = await request.form()
        if not verify_password(form.get("current_password") or "", user.password_hash):
            return templates.TemplateResponse(request, "setup/step.html", _ctx(
                request, session, "security", user=user, enroll=None, fresh=False,
                confirm_skip=False, error="That password isn't right."), status_code=400)
    secret = security.new_totp_secret()
    token = security.sign_pending(config.SECRET_KEY,
                                  {"purpose": "2fa_enroll", "user_id": user.id, "secret": secret})
    resp = _go("/setup/security")
    resp.set_cookie(ENROLL_COOKIE, token, **security.cookie_flags(ENROLL_MAX_AGE))
    return resp


@router.post("/setup/security/confirm")
async def security_confirm(request: Request, session: Session = Depends(get_session)):
    gate = _owner_gate(request)
    if gate:
        return gate
    from app.routers.account import ENROLL_COOKIE, _bump_epoch, _enroll_payload
    user = session.get(models.User, request.state.user.id)
    payload = _enroll_payload(request, user)
    if payload is None or user.totp_enabled:
        return _go("/setup/security")
    form = await request.form()
    if not security.totp_verify(payload["secret"], form.get("code") or ""):
        enroll = {"secret": payload["secret"], "qr": security.qr_svg_data_uri(
            security.provisioning_uri(payload["secret"], user.username or user.name))}
        return templates.TemplateResponse(request, "setup/step.html", _ctx(
            request, session, "security", user=user, enroll=enroll, fresh=True, confirm_skip=False,
            error="That code didn't match. Codes change every 30 seconds; try the current one."),
            status_code=400)
    user.totp_secret, user.totp_enabled = payload["secret"], True
    _bump_epoch(request, user)
    codes = security.make_backup_codes()
    for c in codes:
        session.add(models.BackupCode(user_id=user.id, code_hash=security.hash_code(c)))
    models.audit(session, user.id, "auth.2fa_enabled", detail="setup")
    sk = setup_flow.skipped(session) - {"security"}
    models.set_setting(session, "setup_skipped", ",".join(sorted(sk)))
    models.set_setting(session, "setup_step", str(max(3, setup_flow.step_index(session))))
    session.commit()
    resp = templates.TemplateResponse(request, "setup/step.html", _ctx(
        request, session, "security", user=user, enroll=None, fresh=True, confirm_skip=False,
        backup_codes=codes))
    resp.delete_cookie(ENROLL_COOKIE, path="/")
    return resp


@router.post("/setup/security/skip")
def security_skip(request: Request, session: Session = Depends(get_session)):
    gate = _owner_gate(request)
    if gate:
        return gate
    models.audit(session, request.state.user.id, "setup.2fa_skipped")
    return _advance(session, "security", skip=True)


def _tiers(session):
    return session.scalars(select(models.RateTier).order_by(
        models.RateTier.kind, models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)).all()


def _step_ctx(request, session, key):
    ctx = _ctx(request, session, key)
    if key == "business":
        from app.company import company_info
        ctx.update(company=company_info(session), timezones=TIMEZONES, tz=config.TIMEZONE,
                   default_state=models.get_setting(session, "default_state", "") or "")
    elif key == "prices":
        from app import rates, receivables
        ctx.update(tiers=_tiers(session),
                   labour="%g" % (rates.default_labor_rate_cents(session) / 100),
                   markup=models.get_setting(session, "chem_markup_percent", "0") or "0",
                   net=receivables.default_net_days(session))
    elif key == "water":
        targets, bands = chem_targets.current(session)
        ctx.update(chem_rows=[{"spec": s, "target": targets[s.key], "band": bands[s.key]}
                              for s in chem_targets.SPECS],
                   chems=session.scalars(select(models.Chemical).order_by(models.Chemical.name)).all())
    elif key == "look":
        ui = templating.ui_settings(session)
        ctx.update(appearance=ui, palettes=[{"key": p.key, "name": p.name, "v": palettes.preview(p.key, p.base)}
                                            for p in palettes.PALETTES])
    elif key == "crew":
        ctx.update(crew=session.scalars(select(models.User).where(
            models.User.active, models.User.id != request.state.user.id).order_by(models.User.name)).all())
    elif key == "connect":
        ctx.update(smtp=mailer.smtp_settings(session), smtp_ok=mailer.smtp_configured(session),
                   owner_email=models.get_setting(session, "owner_email", "") or "")
    elif key == "review":
        from app import rates, receivables
        from app.company import company_info
        ui = templating.ui_settings(session)
        ctx.update(company=company_info(session), tiers=_tiers(session),
                   labour="%g" % (rates.default_labor_rate_cents(session) / 100),
                   markup=models.get_setting(session, "chem_markup_percent", "0") or "0",
                   net=receivables.default_net_days(session),
                   chem_count=len([c for c in session.scalars(select(models.Chemical)).all() if c.active]),
                   pal_name=("Your colour" if ui["palette"] == "custom"
                             else palettes.BY_KEY.get(ui["palette"], palettes.BY_KEY["lagoon"]).name),
                   nav_name="top bar" if ui["nav_layout"] == "top" else "sidebar",
                   crew=session.scalars(select(models.User).where(
                       models.User.active, models.User.id != request.state.user.id)).all(),
                   smtp_ok=mailer.smtp_configured(session),
                   user=session.get(models.User, request.state.user.id), tz=config.TIMEZONE)
    return ctx


# ---------- the upgraded owner's profile ----------

@router.get("/setup/profile")
def profile_form(request: Request, session: Session = Depends(get_session)):
    user = request.state.user
    if user is None or user.role != "admin" or not request.app.state.setup_legacy:
        return _go("/")
    return templates.TemplateResponse(request, "setup/profile.html",
                                      {"form": {"name": "" if user.name == "Owner" else user.name,
                                                "username": "" if user.username == "owner" else user.username},
                                       "error": None})


@router.post("/setup/profile")
async def profile_save(request: Request, session: Session = Depends(get_session)):
    me = request.state.user
    if me is None or me.role != "admin" or not request.app.state.setup_legacy:
        return _go("/")
    user = session.get(models.User, me.id)
    form = await request.form()
    name = (form.get("name") or "").strip()[:80]
    username = (form.get("username") or "").strip().lower() or setup_flow.suggest_username(name)
    current = form.get("current_password") or ""
    pw, pw2 = form.get("password") or "", form.get("password_confirm") or ""
    error = None
    if len(name) < 2 or name.lower() == "owner":
        error = "Enter your full name."
    elif not setup_flow.username_ok(username) or username == "owner":
        error = "Pick a username of 3–32 lowercase letters, numbers, dots or dashes (not “owner”)."
    elif session.scalars(select(models.User).where(models.User.username == username,
                                                   models.User.id != user.id)).first():
        error = "That username is taken."
    elif not verify_password(current, user.password_hash):
        error = "Your current password isn't right. It's the APP_PASSWORD you signed in with."
    elif not MIN_PASSWORD <= len(pw) <= MAX_PASSWORD_LEN:
        error = f"Use a new password of {MIN_PASSWORD} to {MAX_PASSWORD_LEN} characters."
    elif pw != pw2:
        error = "The two new passwords don't match."
    if error:
        return templates.TemplateResponse(request, "setup/profile.html",
                                          {"form": {"name": name, "username": username}, "error": error},
                                          status_code=400)
    user.name, user.username, user.password_hash = name, username, hash_password(pw)
    from app.routers.account import _bump_epoch
    _bump_epoch(request, user)
    models.set_setting(session, "setup_status", "done")
    models.audit(session, user.id, "setup.profile_finished", detail=username)
    session.commit()
    request.app.state.setup_legacy = False
    return _go("/dashboard")


# ---------- owner reset (python -m app.manage reset-owner) ----------

@router.get("/setup/reset/{token}")
def reset_form(token: str, request: Request, session: Session = Depends(get_session)):
    user, clear_2fa = setup_flow.redeem_reset(session, token)
    return templates.TemplateResponse(request, "setup/reset.html",
                                      {"ok": user is not None, "user": user, "clear_2fa": clear_2fa,
                                       "token": token, "error": None},
                                      status_code=200 if user else 404)


@router.post("/setup/reset/{token}")
async def reset_save(token: str, request: Request, session: Session = Depends(get_session)):
    user, clear_2fa = setup_flow.redeem_reset(session, token)
    if user is None:
        return templates.TemplateResponse(request, "setup/reset.html",
                                          {"ok": False, "user": None, "clear_2fa": False,
                                           "token": "", "error": None}, status_code=404)
    form = await request.form()
    pw, pw2 = form.get("password") or "", form.get("password_confirm") or ""
    error = None
    if not MIN_PASSWORD <= len(pw) <= MAX_PASSWORD_LEN:
        error = f"Use a password of {MIN_PASSWORD} to {MAX_PASSWORD_LEN} characters."
    elif pw != pw2:
        error = "The two passwords don't match."
    if error:
        return templates.TemplateResponse(request, "setup/reset.html",
                                          {"ok": True, "user": user, "clear_2fa": clear_2fa,
                                           "token": token, "error": error}, status_code=400)
    setup_flow.redeem_reset(session, token, consume=True)
    user.password_hash = hash_password(pw)
    user.session_epoch += 1          # sign out every existing session
    if clear_2fa:
        user.totp_enabled, user.totp_secret = False, None
        for bc in session.scalars(select(models.BackupCode).where(
                models.BackupCode.user_id == user.id)).all():
            session.delete(bc)
    models.audit(session, user.id, "setup.owner_reset", detail="2fa cleared" if clear_2fa else "")
    session.commit()
    return _go("/login?reset=1")


@router.get("/setup/{key}")
def step_page(key: str, request: Request, session: Session = Depends(get_session)):
    if key not in setup_flow.KEYS or key in ("welcome", "account"):
        return _go("/setup")
    gate = _owner_gate(request)
    if gate:
        return gate
    return templates.TemplateResponse(request, "setup/step.html", _step_ctx(request, session, key))


def _num(raw, lo, hi):
    try:
        v = float((raw or "").strip().replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return v if math.isfinite(v) and lo <= v <= hi else None


@router.post("/setup/{key}")
async def step_save(key: str, request: Request, session: Session = Depends(get_session)):
    if key not in setup_flow.KEYS or key in ("welcome", "account", "security"):
        return _go("/setup")
    gate = _owner_gate(request)
    if gate:
        return gate
    form = await request.form()
    if form.get("skip"):
        return _advance(session, key, skip=True)
    if key == "business":
        for k in ("business_name", "company_phone", "company_email", "company_address",
                  "company_license"):
            models.set_setting(session, k, (form.get(k) or "").strip()[:200])
        tz = (form.get("timezone") or "").strip()
        if config.set_timezone(tz):
            models.set_setting(session, "timezone", tz)
        st = (form.get("default_state") or "").strip().upper()
        if len(st) == 2 and st.isalpha() or st == "":
            models.set_setting(session, "default_state", st)
        logo = form.get("logo")
        if logo is not None and getattr(logo, "filename", ""):
            problem = store_logo(await logo.read(), logo.content_type)
            if problem:
                session.commit()
                ctx = _step_ctx(request, session, key)
                ctx["error"] = problem
                return templates.TemplateResponse(request, "setup/step.html", ctx, status_code=400)
    elif key == "prices":
        for t in _tiers(session):
            v = _num(form.get(f"tier_{t.id}"), 0, 100_000)
            if v is not None:
                t.rate_cents = round(v * 100)
        lab = _num(form.get("labour"), 0, 10_000)
        if lab is not None:
            models.set_setting(session, "labor_rate_cents", str(round(lab * 100)))
        mk = _num(form.get("markup"), 0, 1000)
        if mk is not None:
            models.set_setting(session, "chem_markup_percent", "%g" % mk)
        net = _num(form.get("net"), 0, 365)
        if net is not None:
            models.set_setting(session, "invoice_net_days", str(int(net)))
    elif key == "water":
        keep = {int(x) for x in form.getlist("chem") if str(x).isdigit()}
        for c in session.scalars(select(models.Chemical)).all():
            c.active = c.id in keep
    elif key == "look":
        pal = (form.get("palette") or "").strip()
        if pal in palettes.BY_KEY or pal == "custom":
            models.set_setting(session, "palette", pal)
        custom = palettes.clean_hex(form.get("palette_custom"))
        if custom:
            models.set_setting(session, "palette_custom", custom)
        nav = (form.get("nav_layout") or "").strip()
        if nav in templating.NAV_LAYOUTS:
            models.set_setting(session, "nav_layout", nav)
    elif key == "crew":
        created, problems = [], []
        names, users, roles = form.getlist("name"), form.getlist("username"), form.getlist("role")
        for name, uname, role in zip(names, users, roles):
            name = (name or "").strip()[:80]
            if not name:
                continue
            uname = (uname or "").strip().lower() or setup_flow.suggest_username(name)
            if not setup_flow.username_ok(uname) or session.scalars(
                    select(models.User).where(models.User.username == uname)).first():
                problems.append(f"{name}: pick another username")
                continue
            temp = "-".join(secrets.choice(WORDS) for _ in range(3)) + "-" + str(secrets.randbelow(9000) + 1000)
            u = models.User(name=name, username=uname, active=True, must_change_password=True,
                            role="admin" if role == "admin" else "tech",
                            password_hash=hash_password(temp))
            if role == "office":
                u.manage_customers = u.view_all_routes = True
            session.add(u)
            session.flush()
            models.audit(session, request.state.user.id, "user.created", detail=f"{uname} (setup)")
            created.append({"name": name, "username": uname, "password": temp})
        if created or problems:
            sk = setup_flow.skipped(session) - {"crew"}
            models.set_setting(session, "setup_skipped", ",".join(sorted(sk)))
            models.set_setting(session, "setup_step", str(max(8, setup_flow.step_index(session))))
            session.commit()
            ctx = _step_ctx(request, session, key)
            ctx.update(created=created, error="; ".join(problems) or None)
            # Temporary passwords are shown in this response only -- never
            # stored anywhere but as hashes.
            return templates.TemplateResponse(request, "setup/step.html", ctx)
    elif key == "connect":
        host = (form.get("smtp_host") or "").strip()
        if host:
            for k in ("smtp_host", "smtp_user", "smtp_from"):
                models.set_setting(session, k, (form.get(k) or "").strip())
            port = (form.get("smtp_port") or "").strip()
            if port.isdigit():
                models.set_setting(session, "smtp_port", port)
            models.set_setting(session, "smtp_starttls", "1")
            pw = form.get("smtp_pass") or ""
            if pw.strip():
                models.set_setting(session, "smtp_pass", pw)
            models.set_setting(session, "smtp_auth_mode", "password")
    elif key == "review":
        models.set_setting(session, "setup_status", "done")
        models.audit(session, request.state.user.id, "setup.finished")
        session.commit()
        return _go("/dashboard")
    return _advance(session, key)

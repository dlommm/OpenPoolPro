"""SMTP mailer: durable outbox queue + flush.

Credentials (SMTP_USER/SMTP_PASS) are read straight from app.config at send
time and never appear in a template or log line -- see send_message, the
only function in this module that touches smtplib.
"""
import smtplib
import threading
from email.message import EmailMessage

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import config, models

MAX_ATTEMPTS = 5
_flush_lock = threading.Lock()


def _config_conf() -> dict:
    """SMTP config from env/app.config (the fallback when no DB settings exist)."""
    return {"host": config.SMTP_HOST, "port": config.SMTP_PORT, "user": config.SMTP_USER,
            "password": config.SMTP_PASS, "from": config.SMTP_FROM,
            "starttls": config.SMTP_STARTTLS}


def smtp_settings(session, *, force_password: bool = False) -> dict:
    """Resolved SMTP config: DB settings first, env/config as the fallback.

    Display/config resolution only — never touches the network. In
    oauth365 mode (Office 365 Modern Auth) the connection details are pinned
    to Exchange Online and the user/from default to the connected mailbox;
    send paths must use send_conf(), which injects a live access token.
    force_password=True resolves the password-mode values regardless of the
    active mode (the settings page's password form edits those even while
    Office 365 is active).
    """
    g = models.get_setting
    mode = "password" if force_password else g(session, "smtp_auth_mode", "password")
    if mode == "oauth365":
        from app import msoauth
        account = g(session, "smtp_oauth_account", "")
        return {
            "auth_mode": "oauth365",
            "host": msoauth.SMTP_HOST, "port": msoauth.SMTP_PORT,
            "user": account, "password": "",
            "from": g(session, "smtp_from", "") or account,
            "starttls": True,
        }
    try:
        port = int(g(session, "smtp_port", str(config.SMTP_PORT)) or config.SMTP_PORT)
    except ValueError:
        port = config.SMTP_PORT
    user = g(session, "smtp_user", config.SMTP_USER)
    return {
        "auth_mode": "password",
        "host": g(session, "smtp_host", config.SMTP_HOST),
        "port": port,
        "user": user,
        "password": g(session, "smtp_pass", config.SMTP_PASS),
        "from": g(session, "smtp_from", "") or user or config.SMTP_FROM,
        "starttls": g(session, "smtp_starttls", "1" if config.SMTP_STARTTLS else "0") != "0",
    }


def send_conf(session) -> dict:
    """smtp_settings plus, in oauth365 mode, a live access token. The ONLY
    conf builder send paths should use (smtp_settings stays network-free for
    the settings page render)."""
    conf = smtp_settings(session)
    if conf["auth_mode"] == "oauth365":
        from app import msoauth
        conf["oauth_token"] = msoauth.access_token(session)
    return conf


def smtp_configured(session=None) -> bool:
    if session is not None:
        conf = smtp_settings(session)
        if conf["auth_mode"] == "oauth365":
            from app import msoauth
            return msoauth.connected(session)
        return bool(conf["host"])
    return bool(config.SMTP_HOST)


def queue(session: Session, to_addr: str, subject: str, body: str,
          customer_id: int | None = None) -> models.EmailOutbox:
    """Add a pending outbox row (flushed so callers see row.id). Caller commits."""
    row = models.EmailOutbox(customer_id=customer_id, to_addr=to_addr,
                             subject=subject, body=body)
    session.add(row)
    session.flush()
    return row


def send_message(to_addr: str, subject: str, body: str, conf: dict | None = None,
                  attachments: list[tuple[bytes, str]] | None = None) -> None:
    """The only function that touches smtplib -- tests monkeypatch this.
    `conf` is a resolved SMTP config (from smtp_settings); None -> env/config.
    `attachments` is an optional list of (data, filename) pairs attached as
    application/pdf parts; None -> no behavior change for existing callers.
    Raises on failure; never logs or surfaces credentials."""
    conf = conf or _config_conf()
    msg = EmailMessage()
    msg["From"] = conf["from"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if attachments:
        for data, name in attachments:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)

    with smtplib.SMTP(conf["host"], conf["port"], timeout=15) as smtp:
        if conf["starttls"]:
            smtp.starttls()
        if conf.get("auth_mode") == "oauth365" and conf.get("oauth_token"):
            # SASL XOAUTH2 (Office 365 Modern Auth): bearer token, no password.
            auth = f"user={conf['user']}\x01auth=Bearer {conf['oauth_token']}\x01\x01"
            smtp.ehlo()
            smtp.auth("XOAUTH2", lambda challenge=None: auth)
        elif conf["user"]:
            smtp.login(conf["user"], conf["password"])
        smtp.send_message(msg)


def flush_outbox(session: Session, limit: int = 20) -> tuple[int, int]:
    # The scheduler and the manual flush button share a single sender.
    if not _flush_lock.acquire(blocking=False):
        return (0, 0)
    try:
        return _flush_outbox(session, limit)
    finally:
        _flush_lock.release()


def _flush_outbox(session: Session, limit: int = 20) -> tuple[int, int]:
    """Send oldest-first pending rows plus failed rows with attempts < 5.

    Caller commits (same contract as queue/audit). Unconfigured SMTP is a
    no-op: returns (0, 0) without touching any row.
    """
    if not smtp_configured(session):
        return (0, 0)
    try:
        conf = send_conf(session)
    except Exception:
        # Token refresh failed (expired grant, Microsoft unreachable): leave
        # every row pending for the next flush instead of burning attempts.
        return (0, 0)

    rows = session.scalars(
        select(models.EmailOutbox)
        .where(or_(models.EmailOutbox.status == "pending",
                   and_(models.EmailOutbox.status == "failed",
                        models.EmailOutbox.attempts < MAX_ATTEMPTS)))
        .order_by(models.EmailOutbox.created_at, models.EmailOutbox.id)
        .limit(limit)
    ).all()

    sent = failed = 0
    for row in rows:
        from app.followups import may_deliver
        if not may_deliver(session, row):
            row.status = "canceled"
            row.last_error = "Invoice balance, status, or recipient changed after approval."
            session.commit()
            continue
        try:
            send_message(row.to_addr, row.subject, row.body, conf)
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:200]
            row.status = "failed"
            failed += 1
        else:
            row.status = "sent"
            row.sent_at = models.utcnow()
            sent += 1
        # Persist each row's outcome before the next external send. A crash (or
        # a locked-DB commit failure) after N successful sends must not leave
        # those N rows "pending" — the next flush would re-send them all to
        # customers. Committing per row makes each send durably at-most-once.
        session.commit()
    return (sent, failed)


def flush_job(engine) -> None:
    """APScheduler entry point: mirrors app.maintenance.run_nightly's shape --
    opens its own Session, flushes the outbox, and commits."""
    with Session(engine) as s:
        flush_outbox(s)
        s.commit()

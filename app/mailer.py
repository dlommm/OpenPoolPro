"""SMTP mailer: durable outbox queue + flush.

Credentials (SMTP_USER/SMTP_PASS) are read straight from app.config at send
time and never appear in a template or log line -- see send_message, the
only function in this module that touches smtplib.
"""
import smtplib
from email.message import EmailMessage

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import config, models

MAX_ATTEMPTS = 5


def _config_conf() -> dict:
    """SMTP config from env/app.config (the fallback when no DB settings exist)."""
    return {"host": config.SMTP_HOST, "port": config.SMTP_PORT, "user": config.SMTP_USER,
            "password": config.SMTP_PASS, "from": config.SMTP_FROM,
            "starttls": config.SMTP_STARTTLS}


def smtp_settings(session) -> dict:
    """Resolved SMTP config: DB settings first, env/config as the fallback."""
    g = models.get_setting
    try:
        port = int(g(session, "smtp_port", str(config.SMTP_PORT)) or config.SMTP_PORT)
    except ValueError:
        port = config.SMTP_PORT
    user = g(session, "smtp_user", config.SMTP_USER)
    return {
        "host": g(session, "smtp_host", config.SMTP_HOST),
        "port": port,
        "user": user,
        "password": g(session, "smtp_pass", config.SMTP_PASS),
        "from": g(session, "smtp_from", "") or user or config.SMTP_FROM,
        "starttls": g(session, "smtp_starttls", "1" if config.SMTP_STARTTLS else "0") != "0",
    }


def smtp_configured(session=None) -> bool:
    if session is not None:
        return bool(smtp_settings(session)["host"])
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
        if conf["user"]:
            smtp.login(conf["user"], conf["password"])
        smtp.send_message(msg)


def flush_outbox(session: Session, limit: int = 20) -> tuple[int, int]:
    """Send oldest-first pending rows plus failed rows with attempts < 5.

    Caller commits (same contract as queue/audit). Unconfigured SMTP is a
    no-op: returns (0, 0) without touching any row.
    """
    conf = smtp_settings(session)
    if not conf["host"]:
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
    return (sent, failed)


def flush_job(engine) -> None:
    """APScheduler entry point: mirrors app.maintenance.run_nightly's shape --
    opens its own Session, flushes the outbox, and commits."""
    with Session(engine) as s:
        flush_outbox(s)
        s.commit()

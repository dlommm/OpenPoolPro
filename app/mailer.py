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


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST)


def queue(session: Session, to_addr: str, subject: str, body: str,
          customer_id: int | None = None) -> models.EmailOutbox:
    """Add a pending outbox row (flushed so callers see row.id). Caller commits."""
    row = models.EmailOutbox(customer_id=customer_id, to_addr=to_addr,
                             subject=subject, body=body)
    session.add(row)
    session.flush()
    return row


def send_message(to_addr: str, subject: str, body: str) -> None:
    """The only function that touches smtplib -- tests monkeypatch this.
    Raises on failure; never logs or surfaces credentials."""
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)


def flush_outbox(session: Session, limit: int = 20) -> tuple[int, int]:
    """Send oldest-first pending rows plus failed rows with attempts < 5.

    Caller commits (same contract as queue/audit). Unconfigured SMTP is a
    no-op: returns (0, 0) without touching any row.
    """
    if not smtp_configured():
        return (0, 0)

    rows = session.scalars(
        select(models.EmailOutbox)
        .where(or_(models.EmailOutbox.status == "pending",
                   and_(models.EmailOutbox.status == "failed",
                        models.EmailOutbox.attempts < MAX_ATTEMPTS)))
        .order_by(models.EmailOutbox.created_at)
        .limit(limit)
    ).all()

    sent = failed = 0
    for row in rows:
        try:
            send_message(row.to_addr, row.subject, row.body)
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

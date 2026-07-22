from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, mailer, models


def _mk_engine_with_schema(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    return engine


def test_queue_flushes_row_caller_commits(engine):
    _mk_engine_with_schema(engine)
    with Session(engine) as s:
        row = mailer.queue(s, "a@x.com", "Subj", "Body")
        assert row.id is not None  # flushed
        assert row.status == "pending"
        s.commit()
    with Session(engine) as s:
        rows = s.scalars(select(models.EmailOutbox)).all()
        assert len(rows) == 1
        assert rows[0].to_addr == "a@x.com" and rows[0].subject == "Subj"


def test_flush_success_marks_sent(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    calls = []

    def fake_send(to_addr, subject, body, conf=None):
        calls.append((to_addr, subject, body))

    monkeypatch.setattr(mailer, "send_message", fake_send)
    with Session(engine) as s:
        mailer.queue(s, "a@x.com", "Subj", "Body")
        s.commit()
        sent, failed = mailer.flush_outbox(s)
        s.commit()
        assert (sent, failed) == (1, 0)
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "sent"
        assert row.sent_at is not None
    assert calls == [("a@x.com", "Subj", "Body")]


def test_flush_failure_records_attempt_and_error(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")

    def fake_send(to_addr, subject, body, conf=None):
        raise RuntimeError("boom" * 100)  # long error, should be truncated to 200 chars

    monkeypatch.setattr(mailer, "send_message", fake_send)
    with Session(engine) as s:
        mailer.queue(s, "a@x.com", "Subj", "Body")
        s.commit()
        sent, failed = mailer.flush_outbox(s)
        s.commit()
        assert (sent, failed) == (0, 1)
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.last_error == ("boom" * 100)[:200]
        assert len(row.last_error) == 200


def test_flush_excludes_failed_rows_at_max_attempts(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "send_message", lambda *a: None)
    with Session(engine) as s:
        s.add(models.EmailOutbox(to_addr="a@x.com", subject="s", body="b",
                                 status="failed", attempts=5, last_error="stuck"))
        s.commit()
        sent, failed = mailer.flush_outbox(s)
        s.commit()
        assert (sent, failed) == (0, 0)
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "failed" and row.attempts == 5  # untouched


def test_flush_unconfigured_smtp_returns_zero_and_untouched(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "")
    called = []
    monkeypatch.setattr(mailer, "send_message", lambda *a: called.append(a))
    with Session(engine) as s:
        mailer.queue(s, "a@x.com", "Subj", "Body")
        s.commit()
        sent, failed = mailer.flush_outbox(s)
        assert (sent, failed) == (0, 0)
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "pending"
    assert called == []


def test_flush_oldest_first_and_limit_respected(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    order = []
    monkeypatch.setattr(mailer, "send_message",
                        lambda to_addr, subject, body, conf=None: order.append(to_addr))
    with Session(engine) as s:
        for i in range(5):
            row = models.EmailOutbox(to_addr=f"n{i}@x.com", subject="s", body="b")
            s.add(row)
            s.flush()
        # created_at all default to utcnow() at flush time (essentially equal);
        # force distinct explicit ordering to make "oldest-first" unambiguous.
        rows = s.scalars(select(models.EmailOutbox).order_by(models.EmailOutbox.id)).all()
        from datetime import timedelta
        base = models.utcnow()
        for i, row in enumerate(rows):
            row.created_at = base - timedelta(minutes=(5 - i))
        s.commit()

        sent, failed = mailer.flush_outbox(s, limit=3)
        s.commit()
        assert (sent, failed) == (3, 0)
    assert order == ["n0@x.com", "n1@x.com", "n2@x.com"]


def test_flush_job_opens_session_and_commits(engine, monkeypatch):
    _mk_engine_with_schema(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "send_message", lambda *a: None)
    with Session(engine) as s:
        mailer.queue(s, "a@x.com", "Subj", "Body")
        s.commit()
    mailer.flush_job(engine)
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "sent"


def test_send_message_builds_email_and_calls_smtp(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "user@example.com")
    monkeypatch.setattr(config, "SMTP_PASS", "secret")
    monkeypatch.setattr(config, "SMTP_FROM", "user@example.com")
    monkeypatch.setattr(config, "SMTP_STARTTLS", True)

    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, user, password):
            events.append(("login", user, password))

        def send_message(self, msg):
            events.append(("send", str(msg["From"]), str(msg["To"]), str(msg["Subject"])))

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    mailer.send_message("dest@x.com", "Hello", "World")
    assert events == [
        ("connect", "smtp.example.com", 587, 15),
        ("starttls",),
        ("login", "user@example.com", "secret"),
        ("send", "user@example.com", "dest@x.com", "Hello"),
    ]


def test_send_message_skips_login_when_no_user(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASS", "")
    monkeypatch.setattr(config, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(config, "SMTP_STARTTLS", False)

    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, user, password):
            events.append(("login",))

        def send_message(self, msg):
            events.append(("send",))

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    mailer.send_message("dest@x.com", "Hello", "World")
    assert events == [("connect", "smtp.example.com", 587, 15), ("send",)]


def test_smtp_configured_reflects_host(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    assert mailer.smtp_configured() is False
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    assert mailer.smtp_configured() is True


def test_save_smtp_settings_via_ui(client, engine):
    from sqlalchemy.orm import Session
    from app import mailer
    r = client.post("/settings/smtp", data={
        "smtp_host": "smtp.test.com", "smtp_port": "465", "smtp_user": "me@test.com",
        "smtp_pass": "secret", "smtp_from": "noreply@test.com", "smtp_starttls": "on"},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        conf = mailer.smtp_settings(s)
        assert conf["host"] == "smtp.test.com" and conf["port"] == 465
        assert conf["user"] == "me@test.com" and conf["password"] == "secret"
        assert conf["from"] == "noreply@test.com" and conf["starttls"] is True
        assert mailer.smtp_configured(s) is True
    # blank password on a later save keeps the stored one (write-only field)
    client.post("/settings/smtp", data={
        "smtp_host": "smtp.test.com", "smtp_port": "465", "smtp_user": "me@test.com",
        "smtp_pass": "", "smtp_from": "noreply@test.com", "smtp_starttls": "on"})
    with Session(engine) as s:
        assert mailer.smtp_settings(s)["password"] == "secret"

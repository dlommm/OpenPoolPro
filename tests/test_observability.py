"""Failure visibility: the point of every assertion here is that something
which used to vanish (a 500's traceback, a scheduler job's exception, a
swallowed badge query) now leaves a line an operator can find in `docker logs`
-- WITHOUT changing what the user sees or flooding this suite's output."""

import contextlib
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app import observability


class _Boom(RuntimeError):
    """Distinctive exception type so we can find it in a formatted traceback."""


def _add_boom_route(app):
    @app.get("/__boom__")
    def boom():
        raise _Boom("kaboom")


@contextlib.contextmanager
def bare_root_logger():
    """Hand configure_logging() an unconfigured root logger to act on, then
    restore what was there -- the rest of the suite relies on pytest's capture
    handler to collect (and suppress) output.

    Has to be entered inside the test body rather than from a fixture: pytest
    re-attaches its capture handler to root between setup and call.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# --- unhandled exceptions -------------------------------------------------

def test_unhandled_exception_logs_traceback_method_and_path(client, caplog):
    _add_boom_route(client.app)
    with caplog.at_level(logging.ERROR, logger="openpoolpro.request"):
        with pytest.raises(_Boom):
            client.get("/__boom__")

    rec = next(r for r in caplog.records if r.name == "openpoolpro.request")
    assert "GET" in rec.getMessage() and "/__boom__" in rec.getMessage()
    formatted = logging.Formatter().format(rec)
    assert "Traceback (most recent call last)" in formatted
    assert "_Boom" in formatted


def test_unhandled_exception_log_names_the_authenticated_user(client, engine, caplog):
    from sqlalchemy.orm import Session

    from app import models

    with Session(engine) as s:
        owner_id = s.query(models.User).filter_by(username="owner").one().id

    _add_boom_route(client.app)
    with caplog.at_level(logging.ERROR, logger="openpoolpro.request"):
        with pytest.raises(_Boom):
            client.get("/__boom__")

    rec = next(r for r in caplog.records if r.name == "openpoolpro.request")
    assert f"user={owner_id}" in rec.getMessage()


def test_exception_is_re_raised_so_the_response_is_unchanged(client):
    """Logging must not turn a 500 into anything else, and the surrounding
    middleware stack must still behave: security headers on a normal page
    proves security_headers is still the outermost layer."""
    _add_boom_route(client.app)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    quiet.cookies.update(client.cookies)

    assert quiet.get("/__boom__").status_code == 500

    ok = client.get("/")
    assert ok.status_code == 200
    assert ok.headers["X-Frame-Options"] == "DENY"
    assert ok.headers["Cache-Control"] == "no-cache"


def test_hsts_follows_cookie_secure(client, monkeypatch):
    """HSTS must ship exactly when the deployment is HTTPS (COOKIE_SECURE is
    the switch, set per-deployment): pinning a browser to HTTPS on a
    deliberately plain-HTTP LAN box would brick the app in that browser for
    180 days. The suite runs with COOKIE_SECURE=False (conftest), so the
    default client must not see the header."""
    import app.config as config

    assert "Strict-Transport-Security" not in client.get("/").headers
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    assert client.get("/").headers["Strict-Transport-Security"] == "max-age=15552000"


def test_cookie_secure_defaults_on(monkeypatch):
    """Secure-by-default: with no COOKIE_SECURE in the environment the session
    cookie must be HTTPS-only. An operator opts OUT for plain HTTP, never in."""
    import importlib

    import app.config as config

    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    try:
        assert importlib.reload(config).COOKIE_SECURE is True
    finally:
        # Re-apply the suite-wide plain-HTTP override the reload wiped out
        # (see conftest.py) — without this every later login silently fails.
        config.COOKIE_SECURE = False


# --- slow requests --------------------------------------------------------

def test_slow_request_logs_a_warning_with_the_duration(client, caplog, monkeypatch):
    # Threshold to 0 rather than sleeping past the real 1s one: this exercises
    # the production middleware in the production stack without a slow test.
    monkeypatch.setattr(observability, "SLOW_REQUEST_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="openpoolpro.request"):
        assert client.get("/").status_code == 200

    rec = next(r for r in caplog.records if r.name == "openpoolpro.request")
    assert rec.levelno == logging.WARNING
    assert "slow request" in rec.getMessage()
    assert "ms" in rec.getMessage()


def test_request_log_never_includes_the_query_string(client, caplog, monkeypatch):
    """Query strings carry reset tokens and the like; a log line is exactly
    where those must not end up."""
    monkeypatch.setattr(observability, "SLOW_REQUEST_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="openpoolpro.request"):
        client.get("/?leaked_token=supersecret")

    assert "supersecret" not in caplog.text
    assert "leaked_token" not in caplog.text
    assert "GET /" in caplog.text


# --- configure_logging ----------------------------------------------------

def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    with bare_root_logger() as root:
        observability.configure_logging()
        observability.configure_logging()

        assert len(root.handlers) == 1
        assert root.handlers[0].stream is sys.stdout  # Docker only keeps stdout
        assert root.level == logging.INFO


def test_configure_logging_defers_to_an_already_configured_root():
    """Why the suite stays quiet: pytest's capture handler gets there first."""
    with bare_root_logger() as root:
        existing = logging.NullHandler()
        root.addHandler(existing)
        observability.configure_logging()

        assert root.handlers == [existing]


@pytest.mark.parametrize("value,expected", [
    ("DEBUG", logging.DEBUG),
    ("warning", logging.WARNING),
    ("not-a-level", logging.INFO),
])
def test_log_level_comes_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("LOG_LEVEL", value)
    with bare_root_logger() as root:
        observability.configure_logging()
        assert root.level == expected


# --- scheduler jobs -------------------------------------------------------

def test_failing_job_is_logged_with_context_and_does_not_propagate(caplog):
    def nightly(engine):
        raise _Boom("VACUUM failed")

    with caplog.at_level(logging.INFO, logger="openpoolpro.job"):
        assert observability.logged_job(nightly, "nightly-backup")("engine") is None

    messages = [r.getMessage() for r in caplog.records if r.name == "openpoolpro.job"]
    assert "job nightly-backup started" in messages
    failure = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert "nightly-backup" in failure.getMessage() and "FAILED" in failure.getMessage()
    assert "_Boom" in logging.Formatter().format(failure)


def test_successful_job_logs_start_and_finish_and_returns_its_result(caplog):
    with caplog.at_level(logging.INFO, logger="openpoolpro.job"):
        assert observability.logged_job(lambda engine: 7, "mailer-flush")("engine") == 7

    messages = [r.getMessage() for r in caplog.records if r.name == "openpoolpro.job"]
    assert "job mailer-flush started" in messages
    assert any(m.startswith("job mailer-flush finished") for m in messages)


def test_create_app_wraps_every_scheduler_job(engine, monkeypatch, tmp_path):
    """Including the backup job, which app.backup.start_scheduler registers
    itself -- create_app has to re-point it, so guard that it really happens."""
    import app.config as config
    from app import mailer
    from app.backup import run_backup
    from app.main import create_app
    from app.maintenance import run_nightly

    monkeypatch.setattr(config, "RUN_SCHEDULER", True)
    monkeypatch.setattr(config, "APP_PASSWORD", "testpass")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    app = create_app(engine=engine)
    try:
        wrapped = {getattr(j.func, "__wrapped__", None)
                   for j in app.state.scheduler.get_jobs()}
        assert {run_backup, run_nightly, mailer.flush_job} <= wrapped
        assert None not in wrapped  # i.e. no job escapes the wrapper
    finally:
        app.state.scheduler.shutdown(wait=False)


# --- the badge that used to fail silently ---------------------------------

def test_low_stock_badge_failure_is_logged_but_still_degrades(client, caplog, monkeypatch):
    from app import inventory

    def explode(session):
        raise _Boom("database is locked")

    monkeypatch.setattr(inventory, "low_stock", explode)
    with caplog.at_level(logging.WARNING, logger="openpoolpro.app"):
        resp = client.get("/")

    assert resp.status_code == 200  # the page must not 500 over a nav badge
    rec = next(r for r in caplog.records if r.name == "openpoolpro.app")
    assert rec.levelno == logging.WARNING
    assert "low-stock" in rec.getMessage()
    assert "_Boom" in logging.Formatter().format(rec)

"""Logging and failure visibility.

Everything here writes to STDOUT and nowhere else. The container is recreated
on every `:latest` pull, so a log file inside it is gone by the time anyone
asks what happened at 2am -- stdout is the one stream Docker keeps (and the
one an operator can `docker logs`/ship elsewhere without app changes).

The three things this module exists to stop losing:
  * a 500 whose traceback only ever reached uvicorn's stderr,
  * a scheduler job that raised into APScheduler's unread logger,
  * a request that got slow enough to matter but never got slow enough to fail.
"""

import functools
import logging
import os
import sys
import time

# Sub-loggers of one root name so an operator can grep a single prefix, and so
# raising/lowering "openpoolpro" moves the whole app at once while third-party
# loggers (uvicorn, apscheduler, sqlalchemy) keep their own levels.
request_log = logging.getLogger("openpoolpro.request")
job_log = logging.getLogger("openpoolpro.job")

# Above this, a request is worth looking at even though it succeeded: on a
# single-box SQLite deployment a multi-second page almost always means lock
# contention or an accidental N+1, both of which get worse silently.
# Read per-request (not captured at wiring time) so it stays tunable.
SLOW_REQUEST_SECONDS = float(os.environ.get("SLOW_REQUEST_SECONDS", "1.0"))

# Timestamp is local-zone and second-resolution: these lines get read by eye
# next to `docker logs -t` output, not parsed by a log aggregator.
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Point the root logger at stdout at LOG_LEVEL (default INFO).

    No-op when the root logger already has handlers. That covers three cases
    with one rule: a second create_app() in the same process must not double
    every line, an operator running uvicorn with --log-config owns the
    configuration, and pytest's capture handler must stay the only handler so
    the suite's output isn't flooded.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(handler)
    root.setLevel(level)


def _user_id(request):
    """Whodunnit, when there is one. require_auth sets request.state.user on
    every non-exempt request; a 500 on /login or /static legitimately has none."""
    user = getattr(request.state, "user", None)
    return getattr(user, "id", None)


def redact_path(path: str) -> str:
    """Strip the bearer credential from /sign paths before they hit a log.

    The e-sign token lives in the URL path — the one secret this module's
    method-and-path-only discipline doesn't automatically cover. Anyone with
    log access could otherwise lift a live signing link. (uvicorn's access
    log is disabled in the Dockerfile for the same reason.)"""
    if path.startswith("/sign/"):
        parts = path.split("/")
        parts[2] = "[token]"
        return "/".join(parts)
    return path


def request_logging_middleware(slow_seconds: float | None = None):
    """Build the http middleware that makes request failures visible.

    Deliberately logs METHOD and PATH only -- never the query string, body,
    form fields or cookies. Half the state-changing routes here carry a
    password, a TOTP code or a session cookie, and a log line is the one place
    those must never end up.
    """

    async def observe_requests(request, call_next):
        started = time.perf_counter()
        path = redact_path(request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            # Log-and-re-raise: Starlette's own error handling still produces
            # the exact same 500, we only leave a copy of the traceback behind.
            request_log.exception(
                "unhandled exception on %s %s (user=%s) after %.0fms",
                request.method, path, _user_id(request),
                (time.perf_counter() - started) * 1000)
            raise
        elapsed = time.perf_counter() - started
        threshold = SLOW_REQUEST_SECONDS if slow_seconds is None else slow_seconds
        if elapsed >= threshold:
            request_log.warning("slow request %s %s -> %s in %.0fms",
                                request.method, path,
                                response.status_code, elapsed * 1000)
        else:
            # Per-request logging stays off by default (LOG_LEVEL=DEBUG turns it
            # on) -- an access log nobody reads only buries the lines that matter.
            request_log.debug("%s %s -> %s in %.0fms", request.method,
                              path, response.status_code, elapsed * 1000)
        return response

    return observe_requests


def logged_job(func, name: str | None = None):
    """Wrap an APScheduler job so its outcome is on stdout either way.

    APScheduler already isolates a raising job from the scheduler thread, but
    it reports the failure through its own logger and then reschedules as if
    nothing happened -- so a nightly backup can fail every night in silence.
    The wrapper swallows after logging (with traceback) for the same reason
    APScheduler does: one bad run must not stop the next one.
    """
    label = name or getattr(func, "__name__", repr(func))

    @functools.wraps(func)
    def run_logged(*args, **kwargs):
        job_log.info("job %s started", label)
        started = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        except Exception:
            job_log.exception("job %s FAILED after %.1fs", label,
                              time.perf_counter() - started)
            return None
        job_log.info("job %s finished in %.1fs", label, time.perf_counter() - started)
        return result

    return run_logged

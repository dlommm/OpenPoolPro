"""Security primitives: TOTP 2FA, backup codes, and login rate limiting.

Pure logic, no FastAPI/DB imports here — routers wire this into request
handling and models.audit().
"""
import base64
import io
import json
import secrets
import time

import itsdangerous
import pyotp
import segno

from app import config
from app.auth_core import pwd_context

ISSUER = "OpenPoolPro"
_PENDING_2FA_SALT = "cpp-2fa-pending"


def cookie_flags(max_age: int) -> dict:
    """HttpOnly + SameSite=Lax always; Secure iff COOKIE_SECURE=1.

    Shared by every router that sets a short-lived pre-auth cookie (the
    login-time pending-2FA cookie in app/routers/auth.py, the 2FA-enroll
    cookie in app/routers/account.py) so the flags can't drift out of sync
    between them.
    """
    return dict(httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                max_age=max_age, path="/")


def new_totp_secret() -> str:
    """Generate a new random base32 TOTP secret."""
    return pyotp.random_base32()


def totp_verify(secret: str | None, code: str | None) -> bool:
    """Verify a 6-digit TOTP code against secret, ±1 time-step window.

    Tolerant of bad input: None/blank/non-6-digit code returns False, and a
    malformed secret returns False instead of raising.
    """
    if not secret or not code:
        return False
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


def provisioning_uri(secret: str, username: str) -> str:
    """otpauth:// URI for authenticator apps, issued under the app name."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_svg_data_uri(uri: str) -> str:
    """Render a provisioning URI as an SVG QR code, returned as a base64 data URI."""
    qr = segno.make(uri)
    buf = io.BytesIO()
    qr.save(buf, kind="svg", xmldecl=False, svgns=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def make_backup_codes() -> list[str]:
    """10 random 10-hex-char single-use backup codes (shown once, stored hashed)."""
    return [secrets.token_hex(5) for _ in range(10)]


def hash_code(code: str) -> str:
    """Bcrypt hash of a normalized (lowercased, stripped) code.

    Reuses the app's configured passlib CryptContext (see app.auth_core) so
    backup codes get the same salted, slow-to-brute-force treatment as
    passwords rather than a bare, unsalted digest.
    """
    normalized = code.strip().lower()
    return pwd_context.hash(normalized)


def verify_code(code: str | None, code_hash: str | None) -> bool:
    """Verify a backup code against its bcrypt hash.

    Normalizes the same way ``hash_code`` does (strip + lowercase) before
    comparing. Tolerant of bad input: a None/blank code or an unparsable
    hash returns False instead of raising.
    """
    if not code or not code_hash:
        return False
    normalized = code.strip().lower()
    try:
        return pwd_context.verify(normalized, code_hash)
    except Exception:
        return False


def sign_pending(secret_key: str, payload: dict) -> str:
    """Sign a small JSON payload for a short-lived pre-auth cookie (e.g. the
    2FA-pending stage), the same way the app's session cookie is signed:
    base64-encoded JSON, timestamped and HMAC-signed via itsdangerous.

    The embedded timestamp (not a hand-rolled "issued_at" field) is what
    ``unsign_pending`` checks against ``max_age`` on the way back in.

    Uses URL-safe base64 with padding stripped (like itsdangerous's own
    internal encoding) rather than standard base64: a raw "=" or "/" in a
    cookie value falls outside Python's http.cookies "legal" charset and
    gets silently double-quoted on the way out, which then fails to
    round-trip back through the Cookie header on the next request.
    """
    signer = itsdangerous.TimestampSigner(str(secret_key), salt=_PENDING_2FA_SALT)
    data = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=")
    return signer.sign(data).decode("utf-8")


def unsign_pending(secret_key: str, token: str | None, max_age: int = 300) -> dict | None:
    """Verify + decode a token from ``sign_pending``.

    Returns None if the token is missing, malformed, tampered with, or older
    than ``max_age`` seconds — never raises.
    """
    if not token:
        return None
    signer = itsdangerous.TimestampSigner(str(secret_key), salt=_PENDING_2FA_SALT)
    try:
        data = signer.unsign(token.encode("utf-8"), max_age=max_age)
        padded = data + b"=" * (-len(data) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None


class RequestThrottle:
    """In-process rolling-window request counter, keyed by client IP.

    Blunts single-source floods / scraping / brute-force sweeps: at most
    `limit` requests per `window` seconds per key, else the request is
    rejected. Distinct from RateLimiter (which counts auth FAILURES) — this
    counts every request. Same single-process, opportunistically-pruned,
    monotonic-clock design. NOT a defense against a distributed DDoS, which
    has to be absorbed upstream.
    """

    _PRUNE_THRESHOLD = 4096

    def __init__(self, limit: int, window: int = 60, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, tuple[int, float]] = {}

    def allow(self, key: str) -> bool:
        """Record a hit for key; return False if it exceeds the limit."""
        now = self._clock()
        count, window_start = self._hits.get(key, (0, now))
        if now - window_start > self.window:
            count, window_start = 0, now
        count += 1
        self._hits[key] = (count, window_start)
        if len(self._hits) > self._PRUNE_THRESHOLD:
            for k, (_, ws) in list(self._hits.items()):
                if now - ws > self.window:
                    self._hits.pop(k, None)
        return count <= self.limit


class RateLimiter:
    """In-process, per-key login rate limiter.

    Monotonic-clock based so wall-clock adjustments can't reset a lockout.
    The clock is injectable for tests.

    Fails are counted in a rolling window per key: ``_fails[key] = (count,
    window_start)`` where ``window_start`` is the clock time of that key's
    first fail. If a key comes back to fail again after its window has aged
    past ``lock_seconds`` without ever reaching ``max_fails``, the count
    resets to 1 with a fresh window rather than accumulating forever — this
    keeps a handful of stale, sub-threshold entries from being treated as
    part of a fresh burst long after the fact.

    Memory is bounded opportunistically rather than with a background
    task: once the number of tracked keys exceeds 1024, a ``.fail()`` call
    prunes entries whose window (or lock) has already expired. Keys that
    are actively failing or locked are never pruned.
    """

    _PRUNE_THRESHOLD = 1024

    def __init__(self, max_fails: int = 5, lock_seconds: int = 300, clock=time.monotonic):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._clock = clock
        self._fails: dict[str, tuple[int, float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            # Lock has expired; clear stale state.
            self._locked_until.pop(key, None)
            self._fails.pop(key, None)
            return False
        return True

    def fail(self, key: str) -> None:
        now = self._clock()
        count, window_start = self._fails.get(key, (0, now))
        if now - window_start > self.lock_seconds:
            # Window aged out without reaching the threshold; start fresh.
            count, window_start = 0, now
        count += 1
        self._fails[key] = (count, window_start)
        if count >= self.max_fails:
            self._locked_until[key] = now + self.lock_seconds
        if len(self._fails) > self._PRUNE_THRESHOLD:
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Drop keys whose fail-window or lock has already expired."""
        for key, (_, window_start) in list(self._fails.items()):
            if now - window_start > self.lock_seconds:
                self._fails.pop(key, None)
        for key, until in list(self._locked_until.items()):
            if now >= until:
                self._locked_until.pop(key, None)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)

    def reset_all(self) -> None:
        """Clear every tracked key. Not used on the request path — a
        test-friendly escape hatch so a module-level (process-lifetime)
        limiter's lock state can't leak from one test into the next."""
        self._fails.clear()
        self._locked_until.clear()

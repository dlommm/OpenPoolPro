"""Security primitives: TOTP 2FA, backup codes, and login rate limiting.

Pure logic, no FastAPI/DB imports here — routers wire this into request
handling and models.audit().
"""
import base64
import io
import secrets
import time

import pyotp
import segno

from app.auth_core import pwd_context

ISSUER = "Coastline Pools"


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

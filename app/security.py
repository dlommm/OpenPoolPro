"""Security primitives: TOTP 2FA, backup codes, and login rate limiting.

Pure logic, no FastAPI/DB imports here — routers wire this into request
handling and models.audit().
"""
import base64
import hashlib
import io
import secrets
import time

import pyotp
import segno

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
    """10 random 8-hex-char single-use backup codes (shown once, stored hashed)."""
    return [secrets.token_hex(4) for _ in range(10)]


def hash_code(code: str) -> str:
    """SHA-256 hex digest of a normalized (lowercased, stripped) code."""
    normalized = code.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RateLimiter:
    """In-process, per-key login rate limiter.

    Monotonic-clock based so wall-clock adjustments can't reset a lockout.
    The clock is injectable for tests.
    """

    def __init__(self, max_fails: int = 5, lock_seconds: int = 300, clock=time.monotonic):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._clock = clock
        self._fails: dict[str, int] = {}
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
        count = self._fails.get(key, 0) + 1
        self._fails[key] = count
        if count >= self.max_fails:
            self._locked_until[key] = self._clock() + self.lock_seconds

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)

"""Encryption at rest for the handful of Settings rows that hold credentials.

WHAT THIS PROTECTS AGAINST — and what it doesn't. The key is derived from
SECRET_KEY, which lives in the environment, while the ciphertext lives in the
SQLite database. The nightly backup ships the DATABASE off-box
(BACKUP_SYNC_CMD -> rclone/cloud) but NOT the environment, so a leaked or
misconfigured backup no longer hands over the QuickBooks refresh token, the
Office 365 refresh token, or the SMTP password. It does NOT defend against an
attacker who already has the host/container (they can read SECRET_KEY from the
environment) — that is a different, larger compromise.

Values are stored as "enc.v1.<base64(nonce||ciphertext)>" using AES-256-GCM
(authenticated, so tampering is detected). Anything without that prefix is
treated as legacy plaintext and returned as-is, which keeps existing
deployments working and lets the boot migration re-encrypt in place.
"""
import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import config

log = logging.getLogger("app.secretbox")

PREFIX = "enc.v1."
_INFO = b"openpoolpro-setting-encryption-v1"


def _key() -> bytes:
    """32-byte key derived from SECRET_KEY. Derived per call (cheap) so a
    SECRET_KEY monkeypatched in tests takes effect immediately."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INFO).derive(config.SECRET_KEY.encode("utf-8"))


def encrypt(plaintext: str) -> str:
    """Encrypt a credential for storage. Empty stays empty (an unset setting
    should read as unset, not as an opaque blob)."""
    if not plaintext:
        return ""
    nonce = os.urandom(12)
    blob = nonce + AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt(stored: str) -> str:
    """Decrypt a stored credential.

    Legacy plaintext (no prefix) is returned unchanged. A value that fails to
    decrypt — wrong SECRET_KEY, or tampered ciphertext — reads as EMPTY rather
    than raising: the app then behaves as "not configured" and the owner
    re-enters the credential, instead of every page 500ing. Rotating
    SECRET_KEY already invalidates sessions, so this matches that expectation.
    """
    if not stored or not stored.startswith(PREFIX):
        return stored or ""
    try:
        blob = base64.urlsafe_b64decode(stored[len(PREFIX):].encode("ascii"))
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], None).decode("utf-8")
    except (InvalidTag, ValueError, TypeError):
        log.warning("stored credential could not be decrypted (SECRET_KEY changed?); "
                    "treating it as unset — re-enter it in Settings")
        return ""


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(PREFIX)

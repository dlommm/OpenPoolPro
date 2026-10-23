import bcrypt

# Work factor for new hashes. Read at call time so tests can lower it
# (tests/conftest.py); existing hashes carry their own cost and verify as-is.
BCRYPT_ROUNDS = 12

# bcrypt only ever looks at the first 72 bytes. passlib (which hashed every
# stored password and backup code before this module called bcrypt directly)
# truncated silently; bcrypt>=5 raises instead. Truncate the same way so
# existing hashes of long passwords still verify.
_BCRYPT_MAX_BYTES = 72

# Cap well below any library limit so an oversized field is a validation
# error at the caller, never a 500.
MAX_PASSWORD_LEN = 128


def _secret_bytes(secret: str) -> bytes:
    return secret.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_secret(secret: str) -> str:
    """Salted bcrypt hash of a password or backup code ($2b$ format)."""
    return bcrypt.hashpw(_secret_bytes(secret), bcrypt.gensalt(BCRYPT_ROUNDS)).decode("ascii")


def verify_secret(secret: str, hashed: str) -> bool:
    """Check a secret against a bcrypt hash; a malformed hash is a mismatch."""
    try:
        return bcrypt.checkpw(_secret_bytes(secret), hashed.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False


def hash_password(pw: str) -> str:
    if len(pw or "") > MAX_PASSWORD_LEN:
        raise ValueError(f"password must be {MAX_PASSWORD_LEN} characters or fewer")
    return hash_secret(pw)


def verify_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return verify_secret(pw, hashed)
    except Exception:
        return False

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# passlib refuses to hash beyond 4096 chars (PasswordSizeError), and bcrypt
# itself silently truncates at 72 bytes. Cap well below the first limit so an
# oversized field is a validation error at the caller, never a 500.
MAX_PASSWORD_LEN = 128


def hash_password(pw: str) -> str:
    if len(pw or "") > MAX_PASSWORD_LEN:
        raise ValueError(f"password must be {MAX_PASSWORD_LEN} characters or fewer")
    return pwd_context.hash(pw)


def verify_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(pw, hashed)
    except Exception:
        return False

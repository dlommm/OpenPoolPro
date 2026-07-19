from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(pw, hashed)
    except Exception:
        return False

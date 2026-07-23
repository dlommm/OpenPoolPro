import os
from zoneinfo import ZoneInfo

APP_VERSION = os.environ.get("APP_VERSION", "dev")  # baked at build time via --build-arg APP_VERSION
DATA_DIR = os.environ.get("DATA_DIR", "./data")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Known placeholder values that MUST NOT reach a real deployment. SECRET_KEY
# signs the session cookie and the pre-auth 2FA tokens, so a shipped default
# lets anyone forge an authenticated session -> full auth bypass. These are
# the module defaults above plus the literals the compose/README templates
# ship, so validate_secrets() catches an operator who never set the env vars.
_INSECURE_SECRET_KEYS = {"", "dev-secret-change-me", "generate-with-openssl-rand-hex-32",
                         "CHANGE_ME_run_openssl_rand_hex_32"}
_INSECURE_APP_PASSWORDS = {"", "changeme", "pick-a-long-password",
                           "CHANGE_ME_owner_login_password"}


def validate_secrets() -> None:
    """Refuse to run a real deployment on placeholder secrets. Called from
    create_app() only when it builds its own file-backed engine (a real boot);
    the test suite passes an in-memory engine and is exempt."""
    problems = []
    if SECRET_KEY in _INSECURE_SECRET_KEYS:
        problems.append("SECRET_KEY is unset or a known placeholder")
    if APP_PASSWORD in _INSECURE_APP_PASSWORDS:
        problems.append("APP_PASSWORD is unset or a known placeholder")
    if problems:
        raise RuntimeError(
            "Refusing to start with insecure configuration: " + "; ".join(problems)
            + ". Set SECRET_KEY (openssl rand -hex 32) and APP_PASSWORD in the environment.")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
TIMEZONE = os.environ.get("APP_TZ", "America/New_York")
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "0") == "1"

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") != "0"


def tz():
    return ZoneInfo(TIMEZONE)

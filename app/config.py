import os
from zoneinfo import ZoneInfo

APP_VERSION = os.environ.get("APP_VERSION", "dev")  # baked at build time via --build-arg APP_VERSION
DATA_DIR = os.environ.get("DATA_DIR", "./data")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
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

import os
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "./data")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
TIMEZONE = os.environ.get("APP_TZ", "America/New_York")
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "0") == "1"


def tz():
    return ZoneInfo(TIMEZONE)

"""Business-timezone display/parse helpers for naive-UTC stored timestamps."""
import re
from datetime import date, datetime, timezone

from app import config

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(config.tz())


def to_local_hhmm(dt: datetime | None) -> str:
    return to_local(dt).strftime("%H:%M") if dt else ""


def from_local_hhmm(d: date, hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    m = _HHMM.match(hhmm.strip())
    if not m:
        return None
    local = datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)),
                     tzinfo=config.tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def local_date(dt: datetime) -> date:
    return to_local(dt).date()

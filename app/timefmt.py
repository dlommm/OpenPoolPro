"""Business-timezone display/parse helpers for naive-UTC stored timestamps."""
import re
from datetime import date, datetime, timezone

from app import config

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(config.tz())


def to_local_hhmm(dt: datetime | None) -> str:
    return to_local(dt).strftime("%H:%M") if dt else ""


def to_local_date(dt: datetime | None) -> str:
    return to_local(dt).strftime("%Y-%m-%d") if dt else ""


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


def parse_ym(raw: str | None, default: tuple[int, int] | None = None) -> tuple[int, int]:
    """Tolerant "YYYY-M" -> (year, month) parser.

    Falls back to `default` if given, otherwise to business-today's
    (year, month), whenever `raw` is missing, malformed, has an
    out-of-range month, or has a year outside 2000..2100.
    """
    if raw:
        try:
            y, m = raw.split("-")
            y, m = int(y), int(m)
            if 1 <= m <= 12 and 2000 <= y <= 2100:
                return y, m
        except ValueError:
            pass
    if default is not None:
        return default
    today = datetime.now(config.tz())
    return today.year, today.month

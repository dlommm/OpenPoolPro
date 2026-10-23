"""Import water tests from a file exported by a tester's own app.

Covers every tester that can export a spreadsheet, without a vendor API:
eXact iDip (the app emails a .csv), Hanna Lab (CSV export), LaMotte
WaterLink Connect 2 (Spin Touch / ColorQ results to CSV), and LabCOM's own
export. Two layouts exist in the wild and both are handled:

* wide -- one row per test, one column per parameter
          (Date, Time, Customer, Free Chlorine, pH, ...)
* long -- one row per parameter
          (Date, Time, Site, Test/Parameter, Result, Units)

Columns are guessed from their headers and shown to the uploader to confirm
or change before anything is stored.
"""
import csv
import hashlib
import io
import re
from datetime import datetime, time, timezone

from app import config, water_readings

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

# Roles a column can play besides a parameter.
ROLES = [("", "— ignore —"), ("datetime", "Date and time"), ("date", "Date"), ("time", "Time"),
         ("label", "Customer / site / pool name"), ("serial", "Device serial"),
         ("param", "Parameter name (one row per parameter)"),
         ("value", "Result value (one row per parameter)"),
         ("unit", "Units (one row per parameter)")]
PARAM_ROLES = [(f"p:{k}", f"Reading: {label}") for k, label, _ in water_readings.PARAMS]
ALL_ROLES = ROLES + PARAM_ROLES


class TesterImportError(Exception):
    pass


def read_rows(data: bytes) -> list[list[str]]:
    if len(data) > MAX_BYTES:
        raise TesterImportError("That file is over 2 MB — export a shorter date range.")
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    except csv.Error:
        raise TesterImportError("That file isn't readable as CSV.") from None
    # Some exports put a title or report header above the table: start at the
    # first row that looks like a header (several non-numeric cells).
    for i, row in enumerate(rows[:15]):
        texty = [c for c in row if c.strip() and water_readings.parse_number(c) is None]
        if len(texty) >= 2 and i + 1 < len(rows):
            rows = rows[i:]
            break
    if len(rows) < 2:
        raise TesterImportError("No test rows found — the file is empty or has only a header.")
    if len(rows) > MAX_ROWS + 1:
        raise TesterImportError(f"That file has more than {MAX_ROWS} rows — export a shorter date range.")
    width = len(rows[0])
    return [r + [""] * (width - len(r)) if len(r) < width else r[:width] for r in rows]


def guess_roles(headers: list[str]) -> list[str]:
    roles = []
    for h in headers:
        low = h.strip().lower()
        squashed = re.sub(r"[^a-z]", "", low)
        if squashed in ("datetime", "timestamp", "dateandtime", "measuredat", "testedat", "datetested"):
            role = "datetime"
        elif squashed in ("date", "testdate", "day"):
            role = "date"
        elif squashed in ("time", "testtime"):
            role = "time"
        elif squashed in ("customer", "site", "location", "pool", "account", "name", "customername",
                          "sitename", "source", "watersource", "tank", "client", "sample", "samplename"):
            role = "label"
        elif "serial" in squashed or squashed in ("device", "deviceid", "instrument", "meter"):
            role = "serial"
        elif squashed in ("parameter", "test", "testname", "analyte", "method", "measurement"):
            role = "param"
        elif squashed in ("result", "value", "reading", "results"):
            role = "value"
        elif squashed in ("unit", "units", "uom"):
            role = "unit"
        else:
            key = water_readings.param_key(h)
            role = f"p:{key}" if key else ""
        roles.append(role)
    return roles


def _is_fahrenheit(header: str, unit: str = "") -> bool:
    text = f"{header} {unit}".lower()
    return "°f" in text or "(f)" in text or "fahrenheit" in text or re.search(r"\bdeg\s*f\b", text) is not None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%I:%M%p")


def parse_when(date_s: str, time_s: str = "") -> datetime | None:
    """Local wall-clock date/time from the file -> naive UTC. US month/day
    order, since these are US testers; ISO dates always work."""
    date_s, time_s = (date_s or "").strip(), (time_s or "").strip()
    if not date_s:
        return None
    # A single datetime cell: try ISO first, then split date from time.
    try:
        dt = datetime.fromisoformat(date_s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(tzinfo=config.tz()).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass
    if not time_s:
        parts = date_s.split(" ", 1)
        if len(parts) == 2 and re.search(r"\d:\d", parts[1]):
            date_s, time_s = parts
    day = None
    for fmt in _DATE_FORMATS:
        try:
            day = datetime.strptime(date_s, fmt).date()
            break
        except ValueError:
            continue
    if day is None:
        return None
    clock = time(12, 0)
    if time_s:
        for fmt in _TIME_FORMATS:
            try:
                clock = datetime.strptime(time_s.upper(), fmt).time()
                break
            except ValueError:
                continue
        else:
            return None
    local = datetime.combine(day, clock).replace(tzinfo=config.tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def build_tests(headers: list[str], rows: list[list[str]], roles: list[str]) -> tuple[list[dict], int]:
    """Turn rows into tests using the confirmed column roles.
    Returns (tests, skipped_rows). Each test: measured_at, label, serial, values."""
    col = {r: i for i, r in enumerate(roles) if r and not r.startswith("p:")}
    params = {i: r[2:] for i, r in enumerate(roles) if r.startswith("p:")}
    if "datetime" not in col and "date" not in col:
        raise TesterImportError("Choose which column holds the test date.")
    long_form = "param" in col and "value" in col
    if not long_form and not params:
        raise TesterImportError("Choose at least one reading column, or the parameter and result columns.")

    def cell(row, role):
        return row[col[role]].strip() if role in col else ""

    tests: dict[tuple, dict] = {}
    skipped = 0
    for row in rows:
        when = parse_when(cell(row, "datetime") or cell(row, "date"), cell(row, "time"))
        if when is None:
            skipped += 1
            continue
        label, serial = cell(row, "label"), cell(row, "serial")
        found: dict = {}
        if long_form:
            key = water_readings.param_key(cell(row, "param"))
            val = water_readings.parse_number(cell(row, "value"))
            if key and val is not None:
                if key == "temp_c" and _is_fahrenheit(cell(row, "param"), cell(row, "unit")):
                    val = round((val - 32) * 5 / 9, 2)
                found[key] = val
        for i, key in params.items():
            val = water_readings.parse_number(row[i])
            if val is None:
                continue
            if key == "temp_c" and _is_fahrenheit(headers[i]):
                val = round((val - 32) * 5 / 9, 2)
            found[key] = val
        if not found:
            skipped += 1
            continue
        test = tests.setdefault((when, label, serial), {
            "measured_at": when, "label": label, "serial": serial, "values": {}})
        for k, v in found.items():
            test["values"].setdefault(k, v)
    return list(tests.values()), skipped


def external_id(test: dict) -> str:
    """Stable identity so re-importing the same export adds nothing."""
    basis = "|".join([test["measured_at"].isoformat(), water_readings.label_key(test["label"]),
                      test["serial"]] + [f"{k}={v:g}" for k, v in sorted(test["values"].items())])
    return hashlib.sha256(basis.encode()).hexdigest()[:40]

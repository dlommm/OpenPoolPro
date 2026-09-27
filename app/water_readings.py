"""Tester readings: normalize, store, and tie each one to the right pool.

A reading from a tester or monitor never names one of our pools. It is tied
to a pool by exactly one of three explicit acts, and nothing here guesses:

1. link   -- a TesterLink says "this LabCOM account / Ondilo pool / iopool
             pool / file label is that pool"; ingest applies it on arrival.
2. visit  -- the tech, standing at the pool, picks the reading on that
             pool's visit form. Only readings already tied to this pool, or
             unassigned readings from a tester registered to this tech (or a
             file this tech uploaded) taken in the last few hours are offered.
3. manual -- an office user assigns it from the water-tests inbox.

A reading tied to one pool is never offered on another pool's visit, and a
reading fills at most one visit (UNIQUE visit_id). When a tester carries a
customer name that does not resemble the stop's customer, the visit form
says so before the tech uses it.
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import models

SOURCES = {
    "labcom": "PoolLab / PoolWaterLAB / PrimeLab (LabCOM)",
    "ondilo": "Ondilo ICO monitor",
    "iopool": "iopool monitor",
    "file": "Imported file",
}

# Parameters in display order: (key, label, unit). The first five are the
# visit form's own fields; the rest are shown with the reading but have no
# column on Visit.
PARAMS = [
    ("fc", "Free chlorine", "ppm"),
    ("ph", "pH", ""),
    ("ta", "Total alkalinity", "ppm"),
    ("cya", "Cyanuric acid", "ppm"),
    ("salt", "Salt", "ppm"),
    ("tc", "Total chlorine", "ppm"),
    ("cc", "Combined chlorine", "ppm"),
    ("ch", "Calcium hardness", "ppm"),
    ("temp_c", "Water temperature", "°C"),
    ("orp", "ORP", "mV"),
]
PARAM_LABELS = {k: label for k, label, _ in PARAMS}
VISIT_FIELDS = ("fc", "ph", "ta", "cya", "salt")

# How far back a tech's own unassigned handheld readings are offered on a
# visit, and how old a pool's already-tied readings may be.
OWN_WINDOW = timedelta(hours=4)
POOL_WINDOW = timedelta(hours=24)
# A reading used at completion must have been taken within this span of the
# visit's finish time. Wide enough for an offline completion replayed hours
# later; narrow enough that last week's test can't pass for today's.
USE_WINDOW = timedelta(hours=12)


def param_key(name: str) -> str | None:
    """Map a vendor's parameter/column name to our key, or None if unknown.

    Order matters: "Total Alkalinity" and "Total Hardness" must not become
    total chlorine, and "Phosphate" must not become pH (hence tokens)."""
    low = (name or "").lower()
    tokens = set(re.findall(r"[a-z]+", low))
    squashed = re.sub(r"[^a-z]", "", low)
    if "alk" in squashed or tokens & {"ta"}:
        return "ta"
    if "hardness" in squashed or "calcium" in squashed or tokens & {"ch", "cah", "th"}:
        return "ch"
    if "cyan" in squashed or "stabili" in squashed or tokens & {"cya", "cys"}:
        return "cya"
    if "salt" in squashed or "nacl" in squashed or "salinity" in squashed or "sodiumchloride" in squashed:
        return "salt"
    if "orp" in tokens or "redox" in squashed:
        return "orp"
    if "temp" in squashed:
        return "temp_c"
    if "ph" in tokens:
        return "ph"
    if "chlor" in squashed or tokens & {"cl", "fc", "tc", "cc", "fcl", "tcl"}:
        if "free" in squashed or tokens & {"fc", "fcl"}:
            return "fc"
        if "combined" in squashed or tokens & {"cc"}:
            return "cc"
        if "total" in squashed or tokens & {"tc", "tcl"}:
            return "tc"
        return "fc"  # a bare "Chlorine" on a pool tester is the free reading
    return None


def parse_number(raw) -> float | None:
    """'7.4', '7,4', '<0.1', '>8', ' 1.20 ppm' -> float; blanks/garbage -> None.
    A bound like '<0.1' keeps its number: the tester could not go lower, and
    the tech sees and can change it before saving."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(raw))
        if not m:
            return None
        val = float(m.group(0).replace(",", "."))
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def fahrenheit(celsius: float) -> float:
    return round(celsius * 9 / 5 + 32, 1)


def display_values(values: dict) -> list[tuple[str, str]]:
    out = []
    for key, label, unit in PARAMS:
        if key not in values or values[key] is None:
            continue
        if key == "temp_c":
            out.append((label, f"{fahrenheit(values[key]):g} °F"))
        else:
            out.append((label, f"{values[key]:g}{(' ' + unit) if unit else ''}"))
    return out


def label_key(label: str) -> str:
    """Normalized key for a free-text label from a file ("Smith , J." and
    "smith j" are one account)."""
    return " ".join(re.findall(r"[a-z0-9]+", (label or "").lower()))[:160]


def _register_device(session: Session, source: str, serial: str) -> None:
    if not serial:
        return
    exists = session.scalar(select(models.TesterDevice.id).where(
        models.TesterDevice.source == source, models.TesterDevice.serial == serial))
    if exists is None:
        session.add(models.TesterDevice(source=source, serial=serial))
        session.flush()


def link_for(session: Session, source: str, external_key: str) -> models.TesterLink | None:
    if not external_key:
        return None
    return session.scalar(select(models.TesterLink).where(
        models.TesterLink.source == source, models.TesterLink.external_key == external_key))


def upsert(session: Session, *, source: str, external_id: str, measured_at: datetime,
           values: dict, kind: str = "handheld", device_serial: str = "",
           external_key: str = "", external_label: str = "",
           uploaded_by_id: int | None = None) -> tuple[models.DeviceReading, bool]:
    """Store one reading idempotently; returns (reading, created).

    A re-sync of the same test (same source + external_id) only ADDS
    parameters that arrived since -- LabCOM delivers one measurement per
    parameter, so a test's pH can land a sync after its chlorine. Values
    already stored are never overwritten."""
    clean = {k: float(v) for k, v in values.items() if v is not None}
    row = session.scalar(select(models.DeviceReading).where(
        models.DeviceReading.source == source, models.DeviceReading.external_id == external_id))
    if row is not None:
        merged = dict(row.values or {})
        added = {k: v for k, v in clean.items() if k not in merged}
        if added:
            merged.update(added)
            row.values = merged
        return row, False
    _register_device(session, source, device_serial)
    row = models.DeviceReading(
        source=source, kind=kind, external_id=external_id[:200], device_serial=device_serial[:80],
        external_key=external_key[:160], external_label=external_label[:200],
        measured_at=measured_at, values=clean, uploaded_by_id=uploaded_by_id)
    link = link_for(session, source, row.external_key)
    if link is not None:
        row.property_id = link.property_id
        row.match_method = "link"
    session.add(row)
    session.flush()
    return row, True


def create_link(session: Session, *, source: str, external_key: str, external_label: str,
                property_id: int, user_id: int | None, secret: str = "") -> models.TesterLink:
    """Tie an external account/pool/label to a pool, and apply it to that
    account's readings still waiting in the inbox. Readings already tied
    elsewhere are left alone: a link is about the future, and silently
    re-pointing past tests would move them between customers."""
    link = link_for(session, source, external_key)
    if link is None:
        link = models.TesterLink(source=source, external_key=external_key[:160])
        session.add(link)
    link.external_label = external_label[:200]
    link.property_id = property_id
    link.created_by_id = user_id
    if secret:
        from app import secretbox
        link.secret = secretbox.encrypt(secret)
    session.flush()
    waiting = session.scalars(select(models.DeviceReading).where(
        models.DeviceReading.source == source,
        models.DeviceReading.external_key == link.external_key,
        models.DeviceReading.property_id.is_(None),
        models.DeviceReading.status == "new")).all()
    for r in waiting:
        r.property_id = property_id
        r.match_method = "link"
        r.assigned_by_id = user_id
    models.audit(session, user_id, "tester.link",
                 f"{source} '{external_label[:60]}' -> property {property_id}; {len(waiting)} waiting reading(s)")
    return link


def link_secret(link: models.TesterLink) -> str:
    if not link.secret:
        return ""
    from app import secretbox
    return secretbox.decrypt(link.secret)


def _owned_by(session: Session, user_id: int):
    """SQL condition: an unassigned reading this user may take onto a visit --
    from a tester registered to them, or a file they uploaded."""
    my_serials = select(models.TesterDevice.serial).where(models.TesterDevice.user_id == user_id)
    return or_(models.DeviceReading.uploaded_by_id == user_id,
               (models.DeviceReading.device_serial != "")
               & models.DeviceReading.device_serial.in_(my_serials))


def candidates(session: Session, visit: models.Visit, user_id: int,
               now: datetime | None = None) -> list[models.DeviceReading]:
    """Readings the tech may use on this visit, newest first."""
    now = now or models.utcnow()
    tied = select(models.DeviceReading).where(
        models.DeviceReading.property_id == visit.property_id,
        models.DeviceReading.status == "new",
        or_(models.DeviceReading.visit_id.is_(None), models.DeviceReading.visit_id == visit.id),
        models.DeviceReading.measured_at >= now - POOL_WINDOW,
        models.DeviceReading.measured_at <= now + timedelta(minutes=10))
    mine = select(models.DeviceReading).where(
        models.DeviceReading.property_id.is_(None),
        models.DeviceReading.status == "new",
        models.DeviceReading.visit_id.is_(None),
        models.DeviceReading.measured_at >= now - OWN_WINDOW,
        models.DeviceReading.measured_at <= now + timedelta(minutes=10),
        _owned_by(session, user_id))
    rows = list(session.scalars(tied)) + list(session.scalars(mine))
    rows.sort(key=lambda r: r.measured_at, reverse=True)
    return rows[:12]


def _words(*parts: str) -> set[str]:
    stop = {"the", "and", "pool", "spa", "home", "house", "main", "road", "street", "ave", "drive", "lane"}
    return {w for p in parts for w in re.findall(r"[a-z0-9]+", (p or "").lower())
            if len(w) >= 3 and w not in stop}


def name_warning(reading: models.DeviceReading, prop: models.Property) -> str:
    """A caution when the tester's own name for this water doesn't look like
    this stop. Empty when there is nothing to compare or the names share a
    meaningful word (surname, street name, house number)."""
    if reading.property_id == prop.id and reading.match_method in ("link", "manual"):
        return ""
    label = (reading.external_label or "").strip()
    if not label:
        return ""
    ours = _words(prop.customer.name, prop.label, prop.street, prop.address, prop.city)
    if _words(label) & ours:
        return ""
    return (f"The tester recorded this as “{label}”, which doesn't match "
            f"{prop.customer.name}. Use it only if you just tested this pool.")


def suggestions(session: Session, reading: models.DeviceReading, limit: int = 3) -> list[models.Property]:
    """Pools whose customer name/address share words with the reading's label
    -- shown to the office as hints, never applied automatically."""
    words = _words(reading.external_label)
    if not words:
        return []
    scored = []
    props = session.scalars(select(models.Property).where(models.Property.active)).all()
    for p in props:
        score = len(words & _words(p.customer.name, p.label, p.street, p.address, p.city))
        if score:
            scored.append((score, p.customer.name, p))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in scored[:limit]]


def attach_to_visit(session: Session, visit: models.Visit, reading_id: str | None,
                    user: models.User) -> str:
    """Tie the reading chosen on the visit form to the completed visit.

    Returns "" on success (or when none was chosen), else the reason it was
    refused. A refusal never blocks the completion: the numbers the tech
    typed are saved either way, the reading just isn't credited as their
    source. Called after the visit's readings are written."""
    current = session.scalar(select(models.DeviceReading).where(
        models.DeviceReading.visit_id == visit.id))
    try:
        rid = int(reading_id) if reading_id else None
    except ValueError:
        rid = None
    if rid is None:
        if current is not None:
            current.edited = _differs(current, visit)
        return ""
    reading = session.get(models.DeviceReading, rid)
    reason = _refusal(session, visit, reading, user)
    if reason:
        models.audit(session, user.id, "tester.use_refused", f"visit {visit.id} reading {rid}: {reason}")
        return reason
    if current is not None and current.id != reading.id:
        current.visit_id = None
        current.edited = False
    if reading.property_id is None:
        reading.property_id = visit.property_id
        reading.match_method = "visit"
        reading.assigned_by_id = user.id
    reading.visit_id = visit.id
    reading.edited = _differs(reading, visit)
    session.flush()
    return ""


def _refusal(session: Session, visit: models.Visit, reading, user: models.User) -> str:
    if reading is None or reading.status != "new":
        return "reading no longer available"
    if reading.visit_id is not None and reading.visit_id != visit.id:
        return "reading already used on another visit"
    if reading.property_id is not None and reading.property_id != visit.property_id:
        return "reading belongs to a different pool"
    if reading.property_id is None:
        owned = session.scalar(select(models.DeviceReading.id).where(
            models.DeviceReading.id == reading.id, _owned_by(session, user.id)))
        if owned is None:
            return "reading is from someone else's tester"
    anchor = visit.completed_at or models.utcnow()
    if abs(reading.measured_at - anchor) > USE_WINDOW:
        return "reading was not taken near this visit"
    return ""


def _differs(reading: models.DeviceReading, visit: models.Visit) -> bool:
    for key in VISIT_FIELDS:
        if key in (reading.values or {}):
            saved = getattr(visit, key)
            if saved is None or abs(saved - reading.values[key]) > 1e-9:
                return True
    return False


def visit_reading(session: Session, visit_id: int) -> models.DeviceReading | None:
    return session.scalar(select(models.DeviceReading).where(models.DeviceReading.visit_id == visit_id))


def prune(session: Session, now: datetime | None = None) -> int:
    """Drop readings no visit used once they are 120 days old. Monitors post
    every hour, and an unused reading older than that answers no question
    the visit history doesn't."""
    now = now or models.utcnow()
    old = session.scalars(select(models.DeviceReading).where(
        models.DeviceReading.visit_id.is_(None),
        models.DeviceReading.measured_at < now - timedelta(days=120))).all()
    for r in old:
        session.delete(r)
    return len(old)

"""Structured US address helpers: compose a display string from parts, and
best-effort parse a free-text address into parts. Parsing is heuristic and
never raises — a miss leaves fields blank for the user to correct."""
import re

_STATE_ZIP = re.compile(r"^(?:(?P<city>.+?)\s+)?(?P<state>[A-Za-z]{2})(?:\s+(?P<zip>\d{5}))?$")


def compose_address(street: str, city: str, state: str, zip: str) -> str:
    street, city, state, zip = ((x or "").strip() for x in (street, city, state, zip))
    tail = " ".join(p for p in (state, zip) if p).strip()
    return ", ".join(p for p in (street, city, tail) if p)


def parse_address(text: str) -> dict:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    text = (text or "").strip()
    if not text:
        return out
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return out
    out["street"] = parts[0]
    if len(parts) == 1:
        return out
    # City may be its own comma-part (3-part form) or embedded in the last part.
    last = parts[-1]
    m = _STATE_ZIP.match(last)
    if len(parts) >= 3:
        out["city"] = parts[1]
        if m:
            out["state"] = (m.group("state") or "").upper()
            out["zip"] = m.group("zip") or ""
        else:
            out["state"] = ""  # leave; unusual format
    elif m:
        out["city"] = (m.group("city") or "").strip()
        out["state"] = (m.group("state") or "").upper()
        out["zip"] = m.group("zip") or ""
    else:
        out["city"] = last
    return out

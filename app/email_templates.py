"""Editable customer emails: an ordered block list per template, one shared
email style, and the uploaded images those blocks show.

A template document is `{"subject": str, "blocks": [block, ...]}`, stored as
JSON in the setting `email_tpl_<key>` (absent = the built-in default). The
same blocks produce both parts of the message: `render()` returns the exact
plain-text body and the resolved blocks the HTML template (emails/branded.html)
draws. Each block says which part it appears in (`show`: both, html, text), so
the defaults reproduce the hard-coded emails byte for byte.

Text rules, shared by every text field of every block:
  {field}         replaced by the value of a field on this template's
                  allow-list. Anything else in braces is left exactly as typed
                  (and flagged by the editor).
  [ ... {field} ] text in square brackets that mentions a field is left out
                  when that field is empty, otherwise the brackets go and the
                  text stays. Brackets without a field stay as typed.
Values are inserted once and never re-scanned, so a customer's name holding
"{x}" or "[y]" prints as is. Everything is plain text: the HTML template
autoescapes it, and links are made only from http(s) URLs.

The email style (`email_style`) holds an accent ("" follows the company
palette, else a validated hex) and a button shape. Images are PNG or JPEG
uploads, re-encoded and stored under DATA_DIR/branding/email (so backups
carry them), referenced by id and embedded as CID parts at send time; never a
remote URL.

No FastAPI imports: the email builders, mailer and routers all use this.
"""
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field

from app import config, models, palettes

SUBJECT_MAX = 200
TEXT_MAX = 2000
LABEL_MAX = 120
ALT_MAX = 200
MAX_BLOCKS = 40
MAX_ROWS = 12
DOC_MAX_BYTES = 64 * 1024
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_MAX_W = 1200
IMAGE_DISPLAY_W = 544  # the 600px sheet less its padding
IMAGE_CID = "opp-img-"

SHOWS = (("both", "Both parts"), ("html", "Branded only"), ("text", "Plain-text copy only"))
ALIGNS = (("left", "Left"), ("center", "Centre"), ("right", "Right"))
BUTTON_SHAPES = {"rounded": "8px", "pill": "999px", "square": "2px"}
STYLE_DEFAULT = {"accent": "", "button": "rounded"}
BUTTON_TEXT = "#FFFFFF"
MIN_BUTTON_CONTRAST = 4.5

# type -> (label, what it is). Order is the add-a-block palette's order.
TYPES = {
    "heading": ("Heading", "A title line"),
    "text": ("Text", "A paragraph with fields"),
    "details": ("Details", "Label and value rows"),
    "readings": ("Visit readings", "Water test, chemicals and tasks, filled in from the visit"),
    "button": ("Button", "Opens a page the app links to"),
    "attachment": ("Attachment note", "Names the attached PDF"),
    "image": ("Image", "A picture you upload, such as a banner"),
    "divider": ("Divider", "A thin line"),
    "signoff": ("Sign-off", "Your shared sign-off, else the company name"),
}
COMMON_TYPES = ("heading", "text", "details", "image", "divider", "signoff")
TEXT_TYPES = ("heading", "text", "button")

_TOKEN = re.compile(r"\{([a-z_]+)\}")
_GROUP_OR_TOKEN = re.compile(r"\[([^\[\]]*)\]|\{([a-z_]+)\}")
_ANY_BRACED = re.compile(r"\{([^{}\s]{1,40})\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_IMAGE_ID = re.compile(r"[0-9a-f]{16}")
_URL = re.compile(r"https?://[^\s<>\"'`]+")


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    when: str
    fields: dict  # field -> label; the allow-list for {tokens} and detail rows
    default: dict
    links: dict = field(default_factory=dict)  # url field -> label (button targets)
    extra_types: tuple = ()
    attachment: bool = False

    @property
    def types(self) -> tuple:
        extra = set(self.extra_types)
        if self.links:
            extra.add("button")
        if self.attachment:
            extra.add("attachment")
        return tuple(t for t in TYPES if t in COMMON_TYPES or t in extra)


def _b(type_, show="both", **kw):
    return {"type": type_, "show": show, **kw}


def _txt(text, show="both"):
    return _b("text", show, text=text, align="left")


def _hd(text):
    return _b("heading", "html", text=text, align="left")


_SIGNOFF = {"signoff": "Your sign-off (blank when none is set)"}

TEMPLATES = {t.key: t for t in (
    Template(
        "visit", "Visit completed", "Sent by itself the first time a visit is marked done.",
        {"company_name": "Company name", "customer_name": "Customer name",
         "property_label": "Property", "visit_date": "Visit date", "technician": "Technician",
         "notes": "Technician's notes", **_SIGNOFF},
        {"subject": "{company_name} — service visit completed", "blocks": [
            _txt("{company_name}\n{property_label} — {visit_date}", "text"),
            _hd("Your service visit is complete"),
            _txt("{technician} finished your service visit at {property_label} on {visit_date}.",
                 "html"),
            _txt("[Notes from your technician: {notes}]", "html"),
            _b("readings"),
            _txt("[Notes:\n  {notes}]", "text"),
            _txt("Serviced by: {technician}", "text"),
            _txt("[{signoff}]", "text"),
            _b("signoff", "html"),
        ]},
        extra_types=("readings",)),
    Template(
        "invoice", "Invoice", "When you email an invoice. The PDF is attached.",
        {"company_name": "Company name", "customer_name": "Customer name",
         "invoice_number": "Invoice number", "period": "Billing period", "total": "Invoice total",
         "due_date": "Due date (blank when none)", "due_date_iso": "Due date as 2026-10-31",
         **_SIGNOFF},
        {"subject": "Invoice {invoice_number} — {period}", "blocks": [
            _hd("Your {period} invoice"),
            _txt("Hello {customer_name},"),
            _txt("Attached is invoice {invoice_number} for {period}, totalling {total}"
                 "[, due {due_date_iso}]."),
            _txt("Thank you for your business."),
            _b("details", "html", title="", rows=[["Invoice", "invoice_number"],
                                                  ["Period", "period"], ["Amount", "total"],
                                                  ["Due", "due_date"]]),
            _b("attachment", "html"),
            _b("signoff"),
        ]},
        attachment=True),
    Template(
        "reminder", "Payment reminder", "When someone presses Send reminder on an unpaid invoice.",
        {"company_name": "Company name", "customer_name": "Customer name",
         "invoice_number": "Invoice number", "balance": "Balance owed",
         "days_overdue": "Days past due (blank when not yet due)",
         "days_overdue_text": "Days past due in words, e.g. 12 days", **_SIGNOFF},
        {"subject": "Reminder: invoice {invoice_number}", "blocks": [
            _hd("A reminder about invoice {invoice_number}"),
            _txt("Hello {customer_name},"),
            _txt("Invoice {invoice_number} has an outstanding balance of {balance}. "
                 "[It is now {days_overdue_text} past due.]"),
            _txt("If you have already sent payment, thank you — please disregard this note."),
            _b("details", "html", title="", rows=[["Invoice", "invoice_number"],
                                                  ["Balance", "balance"],
                                                  ["Days past due", "days_overdue"]]),
            _b("signoff"),
        ]}),
    Template(
        "followup", "Follow-up reminder",
        "Prepared at each follow-up stage and held in Payment follow-ups until approved.",
        {"company_name": "Company name", "customer_name": "Customer name",
         "invoice_number": "Invoice number", "balance": "Balance owed",
         "due_date": "Due date (or “on receipt”)", **_SIGNOFF},
        {"subject": "{company_name} — reminder for invoice {invoice_number}", "blocks": [
            _hd("A reminder about invoice {invoice_number}"),
            _txt("Hello {customer_name},"),
            _txt("Our records show {balance} outstanding on invoice {invoice_number}, "
                 "due {due_date}."),
            _txt("If you have already paid, thank you — please reply with the payment details "
                 "so we can update our records. For payment arrangements or questions, please "
                 "reply to this email."),
            _b("details", "html", title="", rows=[["Invoice", "invoice_number"],
                                                  ["Balance", "balance"],
                                                  ["Was due", "due_date"]]),
            _b("signoff"),
        ]}),
)}


def setting_key(key: str) -> str:
    return f"email_tpl_{key}"


def default_doc(key: str) -> dict:
    return json.loads(json.dumps(TEMPLATES[key].default))


# --- Text -----------------------------------------------------------------

def fill(text: str, values: dict) -> str:
    """Apply the {field} and [optional {field}] rules (see module docstring)."""
    def one(m):
        k = m.group(1)
        return values[k] if k in values else m.group(0)

    def sub(m):
        if m.group(2) is not None:
            k = m.group(2)
            return values[k] if k in values else m.group(0)
        inner = m.group(1)
        known = [k for k in _TOKEN.findall(inner) if k in values]
        if not known:
            return m.group(0)
        if any(values[k] == "" for k in known):
            return ""
        return _TOKEN.sub(one, inner)

    return _GROUP_OR_TOKEN.sub(sub, text or "")


def unknown_tokens(text: str, fields) -> list[str]:
    """Braced words that are not fields of this template, as typed."""
    return sorted({m.group(1) for m in _ANY_BRACED.finditer(text or "")
                   if m.group(1) not in fields})


def _clean_text(raw, limit: int, multiline: bool = True) -> tuple[str, bool]:
    text = str(raw if raw is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    if not multiline:
        text = text.replace("\n", " ")
    text = _CONTROL.sub("", text)
    return text, len(text) <= limit


# --- Validation -----------------------------------------------------------

def clean(key: str, raw) -> tuple[dict, list[str]]:
    """A normalised template document and the reasons it can't be saved
    ([] = valid). Only the allowed block types, fields and choices survive."""
    tpl = TEMPLATES[key]
    errors: list[str] = []
    if not isinstance(raw, dict):
        return default_doc(key), ["The template could not be read."]
    subject, ok = _clean_text(raw.get("subject"), SUBJECT_MAX, multiline=False)
    subject = subject.strip()
    if not ok:
        errors.append(f"The subject is longer than {SUBJECT_MAX} characters.")
    if not subject:
        errors.append("The subject can't be empty.")
    blocks_in = raw.get("blocks")
    if not isinstance(blocks_in, list):
        blocks_in = []
        errors.append("The template has no block list.")
    if len(blocks_in) > MAX_BLOCKS:
        errors.append(f"Up to {MAX_BLOCKS} blocks fit in one email.")
        blocks_in = blocks_in[:MAX_BLOCKS]
    shows, aligns = dict(SHOWS), dict(ALIGNS)
    plain_fields = [f for f in tpl.fields]
    blocks = []
    for n, b in enumerate(blocks_in, 1):
        if not isinstance(b, dict):
            errors.append(f"Block {n} could not be read.")
            continue
        t = b.get("type")
        if t not in tpl.types:
            errors.append(f"Block {n}: that kind of block isn't available in this email.")
            continue
        out = {"type": t, "show": b.get("show") if b.get("show") in shows else "both"}
        if t in TEXT_TYPES:
            text, ok = _clean_text(b.get("text"), TEXT_MAX if t == "text" else LABEL_MAX,
                                   multiline=t == "text")
            if not ok:
                errors.append(f"Block {n}: the text is too long.")
            out["text"] = text
            out["align"] = b.get("align") if b.get("align") in aligns else "left"
        if t == "button":
            if b.get("link") not in tpl.links:
                errors.append(f"Block {n}: choose what the button opens.")
            out["link"] = b.get("link") if b.get("link") in tpl.links else next(iter(tpl.links), "")
        if t == "details":
            title, ok = _clean_text(b.get("title"), LABEL_MAX, multiline=False)
            if not ok:
                errors.append(f"Block {n}: the title is too long.")
            rows = b.get("rows") if isinstance(b.get("rows"), list) else []
            if len(rows) > MAX_ROWS:
                errors.append(f"Block {n}: up to {MAX_ROWS} rows.")
                rows = rows[:MAX_ROWS]
            clean_rows = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    errors.append(f"Block {n}: a row could not be read.")
                    continue
                label, ok = _clean_text(row[0], LABEL_MAX, multiline=False)
                if not ok:
                    errors.append(f"Block {n}: a row label is too long.")
                if row[1] not in plain_fields:
                    errors.append(f"Block {n}: a row shows a field this email doesn't have.")
                    continue
                clean_rows.append([label, row[1]])
            out["title"], out["rows"] = title, clean_rows
        if t == "image":
            img = str(b.get("image") or "")
            if img and not (_IMAGE_ID.fullmatch(img) and image_file(img)):
                errors.append(f"Block {n}: that image is no longer available; upload it again.")
                img = ""
            alt, ok = _clean_text(b.get("alt"), ALT_MAX, multiline=False)
            if not ok:
                errors.append(f"Block {n}: the image description is too long.")
            out["image"], out["alt"] = img, alt
        blocks.append(out)
    doc = {"subject": subject, "blocks": blocks}
    if len(json.dumps(doc)) > DOC_MAX_BYTES:
        errors.append("The template is too large.")
    return doc, errors


def doc(session, key: str) -> dict:
    """The saved template, or the default when unset or unreadable."""
    raw = models.get_setting(session, setting_key(key), "")
    if raw:
        try:
            parsed, errors = clean(key, json.loads(raw))
        except ValueError:
            errors = ["unreadable"]
        if not errors:
            return parsed
    return default_doc(key)


def is_edited(session, key: str) -> bool:
    return bool(models.get_setting(session, setting_key(key), ""))


def save(session, key: str, document: dict) -> None:
    """Store a cleaned document; the default itself is stored as "unset"."""
    if document == TEMPLATES[key].default:
        models.set_setting(session, setting_key(key), "")
    else:
        models.set_setting(session, setting_key(key), json.dumps(document, ensure_ascii=False))


def warnings(key: str, document: dict) -> dict:
    """{block index: [unknown tokens]} plus "subject", for the editor."""
    fields = TEMPLATES[key].fields
    out = {}
    if (u := unknown_tokens(document.get("subject", ""), fields)):
        out["subject"] = u
    for i, b in enumerate(document.get("blocks", [])):
        found = set()
        for k in ("text", "title", "alt"):
            found.update(unknown_tokens(b.get(k, ""), fields))
        for label, _f in b.get("rows", []):
            found.update(unknown_tokens(label, fields))
        if found:
            out[i] = sorted(found)
    return out


# --- Rendering ------------------------------------------------------------

def _readings_text(r: dict) -> str:
    parts = []
    if r.get("readings"):
        parts.append("\n".join(["Water test:"] + [
            f"  {label}: {value}" + (f" ({target})" if target else "")
            for label, value, target in r["readings"]]))
    if r.get("chemicals"):
        parts.append("\n".join(["Chemicals added:"] + [f"  {n}: {a}" for n, a in r["chemicals"]]))
    if r.get("tasks"):
        parts.append("\n".join(["Completed:"] + [f"  {t}" for t in r["tasks"]]))
    return "\n\n".join(parts)


def _readings_sections(r: dict) -> list:
    sections = []
    if r.get("readings"):
        sections.append(("Water test", [(label, value + (f" · {target}" if target else ""))
                                        for label, value, target in r["readings"]]))
    if r.get("chemicals"):
        sections.append(("Chemicals added", [tuple(c) for c in r["chemicals"]]))
    if r.get("tasks"):
        sections.append(("Completed", [(t, "") for t in r["tasks"]]))
    return sections


@dataclass
class Rendered:
    subject: str
    text: str
    blocks: list  # resolved blocks for the HTML template


def render(key: str, document: dict, values: dict, *, closing: str, attachment: str = "",
           readings: dict | None = None, links: dict | None = None) -> Rendered:
    """Both parts of one message from one document. `values` must hold every
    field of the template (strings, "" when empty); unknown keys are ignored."""
    tpl = TEMPLATES[key]
    vals = {k: str(values.get(k, "") or "") for k in tpl.fields}
    links = links or {}
    texts, html = [], []
    for b in document["blocks"]:
        t, show = b["type"], b.get("show", "both")
        plain, out = "", None
        if t == "heading":
            plain = fill(b["text"], vals)
            if plain.strip():
                out = {"type": "heading", "text": plain.strip(), "align": b.get("align", "left")}
        elif t == "text":
            plain = fill(b["text"], vals)
            if plain.strip():
                out = {"type": "text", "text": plain, "align": b.get("align", "left")}
        elif t == "details":
            title = fill(b.get("title", ""), vals).strip()
            rows = [(fill(label, vals).strip(), vals.get(f, "")) for label, f in b.get("rows", [])]
            rows = [(label, v) for label, v in rows if v]
            if rows:
                body = [f"  {label}: {v}" if title else f"{label}: {v}" for label, v in rows]
                plain = "\n".join(([f"{title}:"] if title else []) + body)
                out = {"type": "details", "sections": [(title, rows)]}
        elif t == "readings":
            r = readings or {}
            plain = _readings_text(r)
            if plain:
                out = {"type": "details", "sections": _readings_sections(r)}
        elif t == "button":
            url = links.get(b.get("link", ""), "")
            label = fill(b["text"], vals).strip()
            if label and _URL.fullmatch(url or ""):
                plain = f"{label}: {url}"
                out = {"type": "button", "label": label, "url": url,
                       "align": b.get("align", "left")}
        elif t == "attachment":
            if attachment:
                plain = f"Attached: {attachment}"
                out = {"type": "attachment", "name": attachment}
        elif t == "image":
            info = image_info(b.get("image", ""))
            if info:
                plain = f"[{b['alt']}]" if b.get("alt") else ""
                out = {"type": "image", "image": b["image"], "alt": b.get("alt", ""),
                       "w": info["w"], "h": info["h"]}
        elif t == "divider":
            plain, out = "----", {"type": "divider"}
        elif t == "signoff":
            plain = closing
            lines = [ln for ln in closing.split("\n") if ln.strip()]
            if lines:
                out = {"type": "signoff", "lines": lines}
        if show in ("both", "text") and plain.strip():
            texts.append(plain)
        if show in ("both", "html") and out is not None:
            html.append(out)
    return Rendered(subject=fill(document["subject"], vals), text="\n\n".join(texts), blocks=html)


# --- Email style ----------------------------------------------------------

def style(session) -> dict:
    try:
        raw = json.loads(models.get_setting(session, "email_style", "") or "{}")
    except ValueError:
        raw = {}
    out, _ = clean_style(raw if isinstance(raw, dict) else {})
    return out


def clean_style(raw: dict) -> tuple[dict, list[str]]:
    errors = []
    accent = str(raw.get("accent") or "").strip()
    if accent:
        hexed = palettes.clean_hex(accent)
        if hexed is None:
            errors.append("The accent must be a colour like #1B6C80.")
            accent = ""
        else:
            accent = hexed
    button = raw.get("button") if raw.get("button") in BUTTON_SHAPES else "rounded"
    return {"accent": accent, "button": button}, errors


def button_colour(accent: str) -> str:
    """The accent, darkened just enough that white button text reads on it."""
    if palettes.ratio(accent, BUTTON_TEXT) >= MIN_BUTTON_CONTRAST:
        return accent
    return palettes.toward(accent, "#000000", BUTTON_TEXT, MIN_BUTTON_CONTRAST)


# --- Images ---------------------------------------------------------------

def image_dir() -> str:
    d = os.path.join(config.DATA_DIR, "branding", "email")
    os.makedirs(d, exist_ok=True)
    return d


def image_file(image_id: str) -> str | None:
    if not _IMAGE_ID.fullmatch(image_id or ""):
        return None
    for ext in ("png", "jpg"):
        p = os.path.join(image_dir(), f"{image_id}.{ext}")
        if os.path.isfile(p):
            return p
    return None


def store_image(data: bytes, content_type: str | None) -> tuple[str | None, str | None]:
    """Validate, re-encode and keep an upload: (image id, None) or (None, reason).
    Re-encoding drops anything but the pixels (metadata, trailing payloads)."""
    from PIL import Image, ImageOps

    if content_type not in ("image/png", "image/jpeg"):
        return None, "Images must be PNG or JPEG."
    if not data:
        return None, "That file is empty."
    if len(data) > IMAGE_MAX_BYTES:
        return None, "Images must be under 2 MB."
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in ("PNG", "JPEG"):
            return None, "Images must be PNG or JPEG."
        if img.width * img.height > 40_000_000:
            return None, "That image is too large."
        img = ImageOps.exif_transpose(img)
        img.thumbnail((IMAGE_MAX_W, IMAGE_MAX_W * 2))
        buf = io.BytesIO()
        if fmt == "PNG":
            img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P", "PA") else img.convert("RGB")
            img.save(buf, "PNG", optimize=True)
            ext = "png"
        else:
            img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            ext = "jpg"
    except Exception:
        return None, "That file isn't a readable PNG or JPEG image."
    out = buf.getvalue()
    image_id = hashlib.sha256(out).hexdigest()[:16]
    path = os.path.join(image_dir(), f"{image_id}.{ext}")
    if not os.path.isfile(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, path)
    return image_id, None


def image_info(image_id: str) -> dict | None:
    """{"data", "subtype", "w", "h"} with the display size, or None."""
    path = image_file(image_id)
    if path is None:
        return None
    try:
        from PIL import Image

        with open(path, "rb") as f:
            data = f.read()
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return None
    disp_w = min(IMAGE_DISPLAY_W, w)
    return {"data": data, "subtype": "png" if path.endswith(".png") else "jpeg",
            "w": disp_w, "h": max(1, round(h * disp_w / w))}

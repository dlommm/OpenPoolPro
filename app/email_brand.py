"""Branded emails: one shared HTML template for every message the app sends.

Each message is built once as a `Mail` -- the exact plain-text body plus the
structured pieces (heading, paragraphs, detail rows, button, attachment name)
the HTML part is drawn from. The plain-text part is always sent; the HTML
part is an `add_alternative` built from templates/emails/branded.html:
table-based, inline CSS only, no remote images. The logo, when shown, travels
as a CID-related part (`cid:opp-logo`) attached at send time.

What customers see is set under Settings > System > "What customers see":
  email_brand    "1" use the company palette, "0" standard Lagoon teal
  email_logo     "1" show the company logo, "0" leave it out
  email_format   "html" branded HTML + plain-text copy, "plain" text only
  email_signoff  closing text; "" means the company name

Escaping: every user or customer string reaches the template as plain text
and Jinja autoescapes it. The only CSS values that come from settings are
hex colours derived from a base that passed palettes.clean_hex, and they are
re-checked here before use. Links are http(s) only.

No FastAPI imports: mailer, followups and the routers all use this module.
"""
import base64
import io
import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

import jinja2

from app import models, palettes

FORMATS = ("html", "plain")
SIGNOFF_MAX = 300
SIGNOFF_MAX_LINES = 6
LOGO_CID = "opp-logo"
DEFAULTS = {"email_brand": "1", "email_logo": "1", "email_format": "html", "email_signoff": ""}

_URL = re.compile(r"https?://[^\s<>\"'`]+")
_LOGO_BLOCK = re.compile(r"<!--logo-->.*?<!--/logo-->", re.S)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Standard OpenPoolPro teal (the Lagoon light tokens in css/v2.css), used when
# the company palette is off or is Lagoon itself.
LAGOON = {"accent": "#1B6C80", "tint": "#E3F1F3", "tint_ink": "#0F4C5C",
          "mast_a": "#0E2A3B", "mast_c": "#17677D", "ink": "#0E2A3B"}
NEUTRAL = {"page": "#F1EEE7", "sheet": "#FFFFFF", "muted": "#52626B", "rule": "#EEEAE2"}

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=True, trim_blocks=True, lstrip_blocks=True)


@dataclass
class Mail:
    """One outgoing message. `text` is the plain-text part, sent verbatim."""
    subject: str
    text: str
    heading: str
    paragraphs: list[str] = field(default_factory=list)
    # (title, [(label, value)]); a row with an empty value is a list item.
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    cta: tuple[str, str] | None = None  # (label, http(s) url)
    attachment: str = ""
    closing: str = ""


# --- Settings -------------------------------------------------------------

def clean_signoff(raw) -> str | None:
    """Normalised sign-off text, or None when it is too long to accept."""
    text = _CONTROL.sub("", str(raw or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [ln.rstrip() for ln in text.strip().split("\n")]
    text = "\n".join(lines).strip()
    if len(text) > SIGNOFF_MAX or len(lines) > SIGNOFF_MAX_LINES:
        return None
    return text


def look(session) -> dict:
    """The validated "What customers see" settings."""
    g = models.get_setting
    fmt = g(session, "email_format", DEFAULTS["email_format"])
    return {
        "brand": g(session, "email_brand", DEFAULTS["email_brand"]) != "0",
        "logo": g(session, "email_logo", DEFAULTS["email_logo"]) != "0",
        "format": fmt if fmt in FORMATS else DEFAULTS["email_format"],
        "signoff": clean_signoff(g(session, "email_signoff", "")) or "",
    }


def colours(session, brand: bool) -> dict:
    """Inline-CSS colours for the template. Off, or Lagoon: standard teal."""
    base = None
    if brand:
        pal = models.get_setting(session, "palette", palettes.DEFAULT)
        base = palettes.base_for(pal, models.get_setting(session, "palette_custom", ""))
    if not base:
        return {**NEUTRAL, **LAGOON}
    t = palettes.derive(base, "light")
    picked = {"accent": t["--a"], "tint": t["--as"], "tint_ink": t["--ai"],
              "mast_a": t["--mast-a"], "mast_c": t["--mast-c"], "ink": t["--t"]}
    # Belt and braces: nothing but a clean hex ever reaches a style attribute.
    return {**NEUTRAL, **{k: palettes.clean_hex(v) or LAGOON[k] for k, v in picked.items()}}


def company(session) -> dict:
    return {"name": models.get_setting(session, "business_name", "") or "OpenPoolPro",
            "phone": models.get_setting(session, "company_phone", "") or ""}


def closing(session, company_name: str, signoff: str | None = None) -> str:
    """The closing line app-written emails end with: the sign-off, else the
    company name (what every message ended with before sign-offs existed)."""
    if signoff is None:
        signoff = look(session)["signoff"]
    return signoff or company_name


# --- Logo -----------------------------------------------------------------

@lru_cache(maxsize=4)
def _logo_cached(path: str, mtime: float):
    from PIL import Image

    img = Image.open(path)
    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P", "PA") else img.convert("RGB")
    img.thumbnail((320, 64))  # 2x the displayed size, so it stays crisp
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    w, h = img.size
    disp_h = min(32, max(1, h // 2))
    disp_w = max(1, round(w * disp_h / h))
    return buf.getvalue(), disp_w, disp_h


def logo():
    """(png_bytes, display_width, display_height) for the email logo, or None.
    Best-effort: a missing or unreadable logo simply leaves it out."""
    from app import company as company_mod

    try:
        path = company_mod.logo_path()
        if not os.path.isfile(path):
            return None
        return _logo_cached(path, os.path.getmtime(path))
    except Exception:
        return None


def embed_logo(html: str) -> tuple[str, bytes | None]:
    """At send time: the logo bytes for `cid:opp-logo`, or the HTML with the
    logo block removed when the logo has gone since the message was queued."""
    if f"cid:{LOGO_CID}" not in html:
        return html, None
    found = logo()
    if found is None:
        return _LOGO_BLOCK.sub("", html), None
    return html, found[0]


# --- Rendering ------------------------------------------------------------

def _segments(line: str) -> list[tuple[str, str | None]]:
    """Split a line into (text, href) runs; only http(s) URLs become links."""
    out, pos = [], 0
    for m in _URL.finditer(line):
        url = m.group(0).rstrip(".,;:!?)")
        end = m.start() + len(url)
        if m.start() > pos:
            out.append((line[pos:m.start()], None))
        out.append((url, url))
        pos = end
    if pos < len(line):
        out.append((line[pos:], None))
    return out


def _block(text: str) -> list[list[tuple[str, str | None]]]:
    return [_segments(ln.strip()) for ln in text.strip().split("\n") if ln.strip()]


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def render(mail: Mail, *, colours: dict, company: dict, logo_src: str | None = None,
           logo_size: tuple[int, int] = (32, 32), meta: dict | None = None) -> str:
    cta = None
    if mail.cta and _URL.fullmatch(mail.cta[1] or ""):
        cta = {"label": mail.cta[0], "url": mail.cta[1]}
    return _env.get_template("emails/branded.html").render(
        mail=mail, c=colours, company=company, cta=cta, meta=meta,
        paragraphs=[_block(p) for p in mail.paragraphs],
        closing=[ln for ln in mail.closing.split("\n") if ln.strip()],
        logo_src=logo_src, logo_w=logo_size[0], logo_h=logo_size[1])


def html_for(session, mail: Mail) -> str:
    """The HTML part to send with `mail`, or "" when the company sends plain
    text only. The logo is referenced by CID and attached by the mailer."""
    lk = look(session)
    if lk["format"] != "html":
        return ""
    found = logo() if lk["logo"] else None
    return render(mail, colours=colours(session, lk["brand"]), company=company(session),
                  logo_src=f"cid:{LOGO_CID}" if found else None,
                  logo_size=found[1:] if found else (32, 32))


def preview(session, kind: str, lk: dict) -> str:
    """A full HTML page showing one sample email as a customer would see it,
    for the settings card's sandboxed iframe. Synthetic data only."""
    mail = sample(session, kind, lk["signoff"])
    co = company(session)
    meta = {"from": co["name"], "to": SAMPLE_TO[kind], "subject": mail.subject}
    if lk["format"] != "html":
        return _env.get_template("emails/plain_preview.html").render(mail=mail, meta=meta)
    found = logo() if lk["logo"] else None
    src = f"data:image/png;base64,{base64.b64encode(found[0]).decode()}" if found else None
    return render(mail, colours=colours(session, lk["brand"]), company=co, logo_src=src,
                  logo_size=found[1:] if found else (32, 32), meta=meta)


# --- The messages ---------------------------------------------------------

def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def visit_mail(*, business: str, property_label: str, day: date, tech_name: str,
               readings: list[tuple[str, str, str]], chemicals: list[tuple[str, str]],
               tasks: list[str], notes: str, signoff: str = "") -> Mail:
    """Visit completed. readings are (label, "7.5 ppm", "target 7.5" or "")."""
    when = day.strftime("%B %-d, %Y")
    lines = [business, f"{property_label} — {when}", ""]
    if readings:
        lines += ["Water test:"] + [
            f"  {label}: {value}" + (f" ({target})" if target else "")
            for label, value, target in readings] + [""]
    if chemicals:
        lines += ["Chemicals added:"] + [f"  {name}: {amount}" for name, amount in chemicals] + [""]
    if tasks:
        lines += ["Completed:"] + [f"  {t}" for t in tasks] + [""]
    if notes:
        lines += ["Notes:", f"  {notes}", ""]
    lines.append(f"Serviced by: {tech_name}")
    if signoff:
        lines += ["", signoff]
    sections = []
    if readings:
        sections.append(("Water test", [(label, value + (f" · {target}" if target else ""))
                                        for label, value, target in readings]))
    if chemicals:
        sections.append(("Chemicals added", list(chemicals)))
    if tasks:
        sections.append(("Completed", [(t, "") for t in tasks]))
    paragraphs = [f"{tech_name} finished your service visit at {property_label} on {when}."]
    if notes:
        paragraphs.append(f"Notes from your technician: {notes}")
    return Mail(subject=f"{business} — service visit completed", text="\n".join(lines),
                heading="Your service visit is complete", paragraphs=paragraphs,
                sections=sections, closing=signoff or business)


def invoice_mail(*, company_name: str, customer_name: str, number: str, period: str,
                 total_cents: int, due: date | None, signoff: str = "") -> Mail:
    close = signoff or company_name
    paragraphs = [f"Hello {customer_name},",
                  f"Attached is invoice {number} for {period}, totalling {_money(total_cents)}"
                  + (f", due {due.isoformat()}" if due else "") + ".",
                  "Thank you for your business."]
    rows = [("Invoice", number), ("Period", period), ("Amount", _money(total_cents))]
    if due:
        rows.append(("Due", due.strftime("%B %-d, %Y")))
    return Mail(subject=f"Invoice {number} — {period}", text="\n\n".join(paragraphs + [close]),
                heading=f"Your {period} invoice", paragraphs=paragraphs,
                sections=[("", rows)], attachment=f"{number}.pdf", closing=close)


def reminder_mail(*, company_name: str, customer_name: str, number: str,
                  balance_cents: int, days_overdue: int, signoff: str = "") -> Mail:
    close = signoff or company_name
    overdue_line = (f"It is now {days_overdue} day{'s' if days_overdue != 1 else ''} past due."
                    if days_overdue else "")
    paragraphs = [f"Hello {customer_name},",
                  f"Invoice {number} has an outstanding balance of "
                  f"{_money(balance_cents)}. {overdue_line}",
                  "If you have already sent payment, thank you — please disregard this note."]
    rows = [("Invoice", number), ("Balance", _money(balance_cents))]
    if days_overdue:
        rows.append(("Days past due", str(days_overdue)))
    return Mail(subject=f"Reminder: invoice {number}", text="\n\n".join(paragraphs + [close]),
                heading=f"A reminder about invoice {number}", paragraphs=paragraphs,
                sections=[("", rows)], closing=close)


def followup_mail(*, company_name: str, customer_name: str, number: str,
                  balance_cents: int, due: date | None, signoff: str = "") -> Mail:
    close = signoff or company_name
    due_text = due.strftime("%B %-d, %Y") if due else "on receipt"
    paragraphs = [f"Hello {customer_name},",
                  f"Our records show {_money(balance_cents)} outstanding on invoice {number}, "
                  f"due {due_text}.",
                  "If you have already paid, thank you — please reply with the payment details "
                  "so we can update our records. For payment arrangements or questions, please "
                  "reply to this email."]
    return Mail(subject=f"{company_name} — reminder for invoice {number}",
                text="\n\n".join(paragraphs + [close]),
                heading=f"A reminder about invoice {number}", paragraphs=paragraphs,
                sections=[("", [("Invoice", number), ("Balance", _money(balance_cents)),
                                ("Was due", due_text)])], closing=close)


def sign_request_mail(*, subject: str, body: str, sign_url: str) -> Mail:
    """The admin-edited signing request (sign_messages); its text carries its
    own closing, so no sign-off is added to either part."""
    return Mail(subject=subject, text=body, heading="Your agreement is ready to sign",
                paragraphs=split_paragraphs(body), cta=("Review and sign", sign_url),
                attachment="contract.pdf")


def signed_copy_mail(*, subject: str, body: str) -> Mail:
    return Mail(subject=subject, text=body, heading="Your signed agreement",
                paragraphs=split_paragraphs(body), attachment="signed-contract.pdf")


def smtp_test_mail(*, company_name: str, signoff: str = "") -> Mail:
    close = signoff or company_name
    line = "This is a test email from OpenPoolPro."
    return Mail(subject="OpenPoolPro test email",
                text=line + (f"\n\n{signoff}" if signoff else ""), heading="Test email",
                paragraphs=[line, "If this arrived, your outgoing mail settings work."],
                closing=close)


# --- Previews (synthetic data only) ----------------------------------------

PREVIEW_KINDS = (("visit", "Visit done"), ("invoice", "Invoice"),
                 ("reminder", "Reminder"), ("sign", "Signing"))
SAMPLE_TO = {"visit": "helen.whitfield@example.com", "invoice": "helen.whitfield@example.com",
             "reminder": "manager@coralshores.example.com", "sign": "board@laurelpines.example.com"}


def sample(session, kind: str, signoff: str) -> Mail:
    name = company(session)["name"]
    if kind == "invoice":
        return invoice_mail(company_name=name, customer_name="Helen Whitfield", number="1080",
                            period="September 2026", total_cents=56000,
                            due=date(2026, 10, 31), signoff=signoff)
    if kind == "reminder":
        return reminder_mail(company_name=name, customer_name="Coral Shores HOA", number="1049",
                             balance_cents=61200, days_overdue=12, signoff=signoff)
    if kind == "sign":
        from app import sign_messages

        ctx = {"customer_name": "Laurel Pines Board", "company_name": name, "season_year": 2027,
               "property_address": "12 Laurel Pines Way", "expiry_label": "7 days",
               "sign_url": "https://pools.example.com/sign/sample-link",
               "signer_name": "", "signed_date": ""}
        return sign_request_mail(
            subject=sign_messages.resolved(session, "sign_email_subject", ctx),
            body=sign_messages.resolved(session, "sign_email_body", ctx), sign_url=ctx["sign_url"])
    return visit_mail(business=name, property_label="5 Port Royal Ln", day=date(2026, 10, 23),
                      tech_name="Jordan Reyes",
                      readings=[("Free chlorine", "3.8 ppm", "target 3"), ("pH", "7.5", "target 7.5"),
                                ("Alkalinity", "90 ppm", "target 100")],
                      chemicals=[("Liquid chlorine", "1.5 gal")],
                      tasks=["Skimmed surface", "Brushed walls", "Emptied baskets"],
                      notes="", signoff=signoff)

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
    # Resolved blocks from app.email_templates; None = draw the HTML from the
    # fields above (the e-sign and test emails).
    blocks: list | None = None


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


def colours(session, brand: bool, style: dict | None = None) -> dict:
    """Inline-CSS colours for the template. A custom accent from the email
    style wins; else the company palette; off, or Lagoon: standard teal."""
    from app import email_templates

    st = style or email_templates.style(session)
    base = st["accent"] or None
    if base is None and brand:
        pal = models.get_setting(session, "palette", palettes.DEFAULT)
        base = palettes.base_for(pal, models.get_setting(session, "palette_custom", ""))
    if not base:
        picked = dict(LAGOON)
    else:
        t = palettes.derive(base, "light")
        picked = {"accent": t["--a"], "tint": t["--as"], "tint_ink": t["--ai"],
                  "mast_a": t["--mast-a"], "mast_c": t["--mast-c"], "ink": t["--t"]}
    # Belt and braces: nothing but a clean hex ever reaches a style attribute.
    out = {**NEUTRAL, **{k: palettes.clean_hex(v) or LAGOON[k] for k, v in picked.items()}}
    out["button"] = email_templates.button_colour(out["accent"])
    return out


def button_radius(session, style: dict | None = None) -> str:
    """The email style's button corner (a fixed CSS length, never user text)."""
    from app import email_templates

    st = style or email_templates.style(session)
    return email_templates.BUTTON_SHAPES.get(st.get("button"), "8px")


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


_IMG_REF = re.compile(r"cid:opp-img-([0-9a-f]{16})")


def embed_images(html: str) -> tuple[str, list[tuple[str, bytes, str]]]:
    """At send time: [(cid, bytes, subtype)] for each uploaded image the HTML
    references, with the block of any image deleted since queueing removed."""
    from app import email_templates

    found = []
    for image_id in dict.fromkeys(_IMG_REF.findall(html)):
        info = email_templates.image_info(image_id)
        if info is None:
            html = re.sub(rf"<!--img:{image_id}-->.*?<!--/img:{image_id}-->", "", html, flags=re.S)
        else:
            found.append((f"{email_templates.IMAGE_CID}{image_id}", info["data"], info["subtype"]))
    return html, found


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


def _legacy_blocks(mail: Mail) -> list:
    """The blocks a field-built Mail draws: heading, paragraphs, detail
    tables, attachment, button, closing (the order they always had)."""
    out = [{"type": "heading", "text": mail.heading, "align": "left"}]
    out += [{"type": "text", "text": p, "align": "left"} for p in mail.paragraphs]
    if mail.sections:
        out.append({"type": "details", "sections": mail.sections})
    if mail.attachment:
        out.append({"type": "attachment", "name": mail.attachment})
    if mail.cta and _URL.fullmatch(mail.cta[1] or ""):
        out.append({"type": "button", "label": mail.cta[0], "url": mail.cta[1], "align": "left"})
    lines = [ln for ln in mail.closing.split("\n") if ln.strip()]
    if lines:
        out.append({"type": "signoff", "lines": lines})
    return out


def _image_src(image_id: str, inline: bool) -> str | None:
    from app import email_templates

    if not inline:
        return f"cid:{email_templates.IMAGE_CID}{image_id}"
    info = email_templates.image_info(image_id)
    if info is None:
        return None
    return f"data:image/{info['subtype']};base64,{base64.b64encode(info['data']).decode()}"


def render(mail: Mail, *, colours: dict, company: dict, logo_src: str | None = None,
           logo_size: tuple[int, int] = (32, 32), meta: dict | None = None,
           inline_images: bool = False, button_radius: str = "8px") -> str:
    """The HTML part. Images are `cid:` references (attached by the mailer)
    or, for the settings preview, data URIs. Never a remote URL."""
    blocks = []
    for b in (mail.blocks if mail.blocks is not None else _legacy_blocks(mail)):
        b = dict(b)
        if b["type"] == "text":
            b["paras"] = [_block(p) for p in split_paragraphs(b["text"])]
        elif b["type"] == "button" and not _URL.fullmatch(b.get("url") or ""):
            continue
        elif b["type"] == "image":
            b["src"] = _image_src(b["image"], inline_images)
            if not b["src"]:
                continue
        blocks.append(b)
    colours = {"button": colours.get("accent"), **colours}
    return _env.get_template("emails/branded.html").render(
        mail=mail, c=colours, company=company, meta=meta, blocks=blocks, btn_r=button_radius,
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
                  logo_size=found[1:] if found else (32, 32),
                  button_radius=button_radius(session))


def preview(session, kind: str, lk: dict, doc: dict | None = None, part: str = "",
            style: dict | None = None) -> str:
    """A full HTML page showing one sample email as a customer would see it,
    for the settings card's and the email builder's sandboxed iframes.
    Synthetic data only. `doc` and `style` preview unsaved edits; `part`
    "text" or "html" shows that part whatever the format setting."""
    mail = sample(session, kind, lk["signoff"], doc)
    co = company(session)
    meta = {"from": co["name"], "to": SAMPLE_TO[kind], "subject": mail.subject}
    if part == "text" or (lk["format"] != "html" and part != "html"):
        return _env.get_template("emails/plain_preview.html").render(mail=mail, meta=meta)
    found = logo() if lk["logo"] else None
    src = f"data:image/png;base64,{base64.b64encode(found[0]).decode()}" if found else None
    return render(mail, colours=colours(session, lk["brand"], style), company=co, logo_src=src,
                  logo_size=found[1:] if found else (32, 32), meta=meta, inline_images=True,
                  button_radius=button_radius(session, style))


# --- The messages ---------------------------------------------------------

def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _from_template(key: str, doc: dict | None, values: dict, *, closing: str,
                   attachment: str = "", readings: dict | None = None) -> Mail:
    """One editable email (app.email_templates): both parts from one block
    list. `doc` None is the built-in default, which reproduces the original
    hard-coded wording exactly."""
    from app import email_templates

    document = doc if doc is not None else email_templates.default_doc(key)
    out = email_templates.render(key, document, values, closing=closing,
                                 attachment=attachment, readings=readings)
    heading = next((b["text"] for b in out.blocks if b["type"] == "heading"), "")
    return Mail(subject=out.subject, text=out.text, heading=heading, blocks=out.blocks,
                attachment=attachment, closing=closing)


def visit_mail(*, business: str, property_label: str, day: date, tech_name: str,
               readings: list[tuple[str, str, str]], chemicals: list[tuple[str, str]],
               tasks: list[str], notes: str, signoff: str = "", customer_name: str = "",
               doc: dict | None = None) -> Mail:
    """Visit completed. readings are (label, "7.5 ppm", "target 7.5" or "")."""
    values = {"company_name": business, "customer_name": customer_name,
              "property_label": property_label, "visit_date": day.strftime("%B %-d, %Y"),
              "technician": tech_name, "notes": notes or "", "signoff": signoff}
    return _from_template("visit", doc, values, closing=signoff or business,
                          readings={"readings": readings, "chemicals": chemicals, "tasks": tasks})


def invoice_mail(*, company_name: str, customer_name: str, number: str, period: str,
                 total_cents: int, due: date | None, signoff: str = "",
                 doc: dict | None = None) -> Mail:
    values = {"company_name": company_name, "customer_name": customer_name,
              "invoice_number": number, "period": period, "total": _money(total_cents),
              "due_date": due.strftime("%B %-d, %Y") if due else "",
              "due_date_iso": due.isoformat() if due else "", "signoff": signoff}
    return _from_template("invoice", doc, values, closing=signoff or company_name,
                          attachment=f"{number}.pdf")


def reminder_mail(*, company_name: str, customer_name: str, number: str,
                  balance_cents: int, days_overdue: int, signoff: str = "",
                  doc: dict | None = None) -> Mail:
    values = {"company_name": company_name, "customer_name": customer_name,
              "invoice_number": number, "balance": _money(balance_cents),
              "days_overdue": str(days_overdue) if days_overdue else "",
              "days_overdue_text": (f"{days_overdue} day{'s' if days_overdue != 1 else ''}"
                                    if days_overdue else ""),
              "signoff": signoff}
    return _from_template("reminder", doc, values, closing=signoff or company_name)


def followup_mail(*, company_name: str, customer_name: str, number: str,
                  balance_cents: int, due: date | None, signoff: str = "",
                  doc: dict | None = None) -> Mail:
    values = {"company_name": company_name, "customer_name": customer_name,
              "invoice_number": number, "balance": _money(balance_cents),
              "due_date": due.strftime("%B %-d, %Y") if due else "on receipt",
              "signoff": signoff}
    return _from_template("followup", doc, values, closing=signoff or company_name)


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
             "reminder": "manager@coralshores.example.com",
             "followup": "manager@coralshores.example.com", "sign": "board@laurelpines.example.com"}


def sample(session, kind: str, signoff: str, doc: dict | None = None) -> Mail:
    """Synthetic sample of one message. Without `doc` the saved template is
    used, so the previews show what customers would actually get."""
    from app import email_templates

    name = company(session)["name"]
    if doc is None and kind in email_templates.TEMPLATES:
        doc = email_templates.doc(session, kind)
    if kind == "invoice":
        return invoice_mail(company_name=name, customer_name="Helen Whitfield", number="1080",
                            period="September 2026", total_cents=56000,
                            due=date(2026, 10, 31), signoff=signoff, doc=doc)
    if kind == "reminder":
        return reminder_mail(company_name=name, customer_name="Coral Shores HOA", number="1049",
                             balance_cents=61200, days_overdue=12, signoff=signoff, doc=doc)
    if kind == "followup":
        return followup_mail(company_name=name, customer_name="Coral Shores HOA", number="1049",
                             balance_cents=61200, due=date(2026, 9, 12), signoff=signoff, doc=doc)
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
                      notes="", signoff=signoff, customer_name="Helen Whitfield", doc=doc)

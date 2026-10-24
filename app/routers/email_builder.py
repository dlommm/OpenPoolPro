"""Admin > Emails: the block email builder (app.email_templates).

The whole template travels in one form, so the page works without
JavaScript: every button posts the form with an `op` (move, delete, add a
block, add or remove a detail row, switch the previewed part, send a test,
save) and the server answers with the page redrawn from that draft. Only
"save" stores anything. The preview is the same template the mailer uses,
in a sandboxed iframe (`srcdoc`, so the draft is what it shows). With
JavaScript the page adds drag-to-reorder, field chips and a live preview,
all built on this same form.
"""
import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import email_brand, email_templates, mailer, models, palettes
from app.db import get_session
from app.deps import require_admin
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

T = email_templates
PARTS = ("html", "text")


def _tpl(key: str) -> T.Template:
    if key not in T.TEMPLATES:
        raise HTTPException(status_code=404)
    return T.TEMPLATES[key]


def _new_block(tpl: T.Template, type_: str) -> dict:
    if type_ == "heading":
        return {"type": "heading", "show": "both", "text": "New heading", "align": "left"}
    if type_ == "text":
        return {"type": "text", "show": "both", "text": "Write something to {customer_name}.",
                "align": "left"}
    if type_ == "details":
        first = next(iter(tpl.fields))
        return {"type": "details", "show": "both", "title": "",
                "rows": [[tpl.fields[first], first]]}
    if type_ == "button":
        return {"type": "button", "show": "both", "text": "Open", "align": "left",
                "link": next(iter(tpl.links), "")}
    if type_ == "image":
        return {"type": "image", "show": "both", "image": "", "alt": ""}
    return {"type": type_, "show": "both"}


async def _read_upload(upload) -> tuple[str | None, str | None]:
    data = await upload.read(T.IMAGE_MAX_BYTES + 1)
    return T.store_image(data, upload.content_type)


async def _draft(form, tpl: T.Template) -> tuple[dict, list[int], list[str]]:
    """(raw document, the form ids of its blocks in order, upload problems)."""
    ids = []
    for part in (form.get("order") or "").split(","):
        if part.strip().isdigit() and int(part) not in ids:
            ids.append(int(part))
    blocks, problems = [], []
    for i in ids[:T.MAX_BLOCKS + 5]:
        p = f"b{i}_"
        b = {"type": form.get(p + "type"), "show": form.get(p + "show"),
             "text": form.get(p + "text"), "align": form.get(p + "align"),
             "link": form.get(p + "link"), "title": form.get(p + "title"),
             "rows": [list(r) for r in zip(form.getlist(p + "rl"), form.getlist(p + "rf"))],
             "image": form.get(p + "image"), "alt": form.get(p + "alt")}
        upload = form.get(p + "file")
        if b["type"] == "image" and getattr(upload, "filename", ""):
            image_id, problem = await _read_upload(upload)
            if problem:
                problems.append(problem)
            else:
                b["image"] = image_id
        blocks.append(b)
    return {"subject": form.get("subject"), "blocks": blocks}, ids, problems


def _style_from_form(form) -> tuple[dict, list[str]]:
    mode = form.get("es_mode")
    raw = {"accent": form.get("es_accent") if mode == "custom" else "",
           "button": form.get("es_button")}
    style, errors = T.clean_style(raw)
    if mode == "custom" and not style["accent"] and not errors:
        errors.append("Choose a custom accent colour, or follow the company palette.")
    return style, errors


def _apply(op: str, tpl: T.Template, doc: dict, ids: list[int]) -> int | None:
    """Apply one editing op to the draft in place; the block index to open."""
    name, _, arg = op.partition(":")
    blocks = doc["blocks"]

    def pos(form_id):
        try:
            p = ids.index(int(form_id))
        except ValueError:
            return None
        return p if p < len(blocks) else None

    if name == "add" and arg in tpl.types and len(blocks) < T.MAX_BLOCKS:
        at = next((n for n, b in enumerate(blocks) if b["type"] == "signoff"), len(blocks))
        blocks.insert(at, _new_block(tpl, arg))
        return at
    if name in ("up", "down", "del", "rowadd"):
        p = pos(arg)
        if p is None:
            return None
        if name == "up" and p > 0:
            blocks[p - 1], blocks[p] = blocks[p], blocks[p - 1]
            return p - 1
        if name == "down" and p < len(blocks) - 1:
            blocks[p + 1], blocks[p] = blocks[p], blocks[p + 1]
            return p + 1
        if name == "del":
            del blocks[p]
            return None
        if name == "rowadd" and blocks[p]["type"] == "details" and len(blocks[p]["rows"]) < T.MAX_ROWS:
            first = next(iter(tpl.fields))
            blocks[p]["rows"].append([tpl.fields[first], first])
        return p
    if name == "rowdel":
        form_id, _, row = arg.partition(".")
        p = pos(form_id)
        if p is not None and blocks[p]["type"] == "details" and row.isdigit():
            if int(row) < len(blocks[p]["rows"]):
                del blocks[p]["rows"][int(row)]
        return p
    return None


def _snippet(tpl: T.Template, b: dict) -> str:
    """A one-line reminder of what a block says, for the collapsed list."""
    if b["type"] in T.TEXT_TYPES:
        text = " ".join((b.get("text") or "").split())
    elif b["type"] == "details":
        text = b.get("title") or ", ".join(label for label, _f in b.get("rows", []))
    elif b["type"] == "image":
        text = b.get("alt") or ("Image uploaded" if b.get("image") else "No image yet")
    elif b["type"] == "readings":
        text = "Water test, chemicals added, tasks done"
    elif b["type"] == "attachment":
        text = "Names the attached PDF"
    elif b["type"] == "signoff":
        text = "Your sign-off, else the company name"
    else:
        text = ""
    return text if len(text) <= 90 else text[:89] + "…"


def _style_note(style: dict, session) -> str:
    if not style["accent"]:
        on = models.get_setting(session, "email_brand", "1") != "0"
        return ("Following the company palette." if on else
                "Standard OpenPoolPro teal: the company palette is off for emails.")
    used = palettes.derive(style["accent"], "light")["--a"]
    if used != style["accent"]:
        return (f"Buttons and links use {used}, a little darker than {style['accent']}, "
                "so white button text stays readable.")
    return "Custom accent for emails. White button text reads clearly on it."


def _page(request, session, key: str, doc: dict, style: dict, *, open_at=None,
          errors=(), notice: str = "", part: str = "html", status: int = 200):
    tpl = _tpl(key)
    lk = email_brand.look(session)
    saved = T.doc(session, key)
    preview = email_brand.preview(session, key, lk, doc=doc, part=part, style=style)
    warn = T.warnings(key, doc)
    blocks = []
    for n, b in enumerate(doc["blocks"]):
        info = T.image_info(b["image"]) if b["type"] == "image" and b.get("image") else None
        thumb = (f"data:image/{info['subtype']};base64,{base64.b64encode(info['data']).decode()}"
                 if info else "")
        blocks.append({**b, "n": n, "label": T.TYPES[b["type"]][0], "warn": warn.get(n, []),
                       "thumb": thumb, "snippet": _snippet(tpl, b)})
    return templates.TemplateResponse(request, "settings/emails.html", {
        "key": key, "tpl": tpl, "templates_list": [
            {"key": t.key, "name": t.name, "edited": T.is_edited(session, t.key)}
            for t in T.TEMPLATES.values()],
        "doc": doc, "blocks": blocks, "open_at": open_at, "subject_warn": warn.get("subject", []),
        "types": [(t, *T.TYPES[t]) for t in tpl.types], "shows": T.SHOWS, "aligns": T.ALIGNS,
        "style": style, "style_note": _style_note(style, session),
        "button_shapes": [("rounded", "Rounded"), ("pill", "Pill"), ("square", "Square")],
        "accent_value": style["accent"] or email_brand.colours(session, lk["brand"], style)["accent"],
        "preview": preview, "part": part, "errors": list(errors), "notice": notice,
        "dirty": doc != saved or style != T.style(session),
        "edited": T.is_edited(session, key), "look": lk,
        "test_to": models.get_setting(session, "company_email", ""),
        "limits": {"blocks": T.MAX_BLOCKS, "text": T.TEXT_MAX, "rows": T.MAX_ROWS},
        "fields_json": json.dumps(sorted(tpl.fields)),
    }, status_code=status)


@router.get("/settings/emails")
def emails_index():
    return RedirectResponse("/settings/emails/visit", status_code=303)


@router.get("/settings/emails/{key}", response_class=HTMLResponse)
def email_builder(key: str, request: Request, session: Session = Depends(get_session)):
    _tpl(key)
    q = request.query_params
    notice = {"saved": "Saved. New emails use this template.",
              "reset": "Back to the default wording."}.get(q.get("done", ""), "")
    part = q.get("part") if q.get("part") in PARTS else "html"
    open_at = int(q["open"]) if (q.get("open") or "").isdigit() else None
    return _page(request, session, key, T.doc(session, key), T.style(session),
                 open_at=open_at, notice=notice, part=part)


def _send_test(session, key: str, doc: dict, style: dict) -> tuple[str, list[str]]:
    to_addr = models.get_setting(session, "company_email", "").strip()
    if not to_addr or "@" not in to_addr:
        return "", ["Add the company email under Business first; tests go there."]
    if not mailer.smtp_configured(session):
        return "", ["Outgoing mail isn't set up yet (Admin > System > Email)."]
    lk = email_brand.look(session)
    mail = email_brand.sample(session, key, lk["signoff"], doc)
    html = ""
    if lk["format"] == "html":
        found = email_brand.logo() if lk["logo"] else None
        html = email_brand.render(
            mail, colours=email_brand.colours(session, lk["brand"], style),
            company=email_brand.company(session),
            logo_src=f"cid:{email_brand.LOGO_CID}" if found else None,
            logo_size=found[1:] if found else (32, 32),
            button_radius=email_brand.button_radius(session, style))
    mailer.queue(session, to_addr, f"[Test] {mail.subject}", mail.text, html=html)
    session.commit()
    sent, failed = mailer.flush_outbox(session)
    session.commit()
    if failed and not sent:
        return "", ["The test didn't send. See the outbox under Admin > System > Email."]
    return f"Test sent to {to_addr} with sample data.", []


@router.post("/settings/emails/{key}", response_class=HTMLResponse)
async def email_builder_post(key: str, request: Request, session: Session = Depends(get_session)):
    tpl = _tpl(key)
    form = await request.form()
    raw, ids, problems = await _draft(form, tpl)
    doc, errors = T.clean(key, raw)
    style, style_errors = _style_from_form(form)
    op = str(form.get("op") or "save")
    part = form.get("part") if form.get("part") in PARTS else "html"
    if op.startswith("part:") and op[5:] in PARTS:
        part = op[5:]
    if op == "save":
        errors = problems + errors + style_errors
        if errors:
            return _page(request, session, key, doc, style, errors=errors, part=part, status=400)
        T.save(session, key, doc)
        default_style = style == T.STYLE_DEFAULT
        models.set_setting(session, "email_style", "" if default_style else json.dumps(style))
        models.audit(session, request.state.user.id, "settings.email_template", detail=key)
        session.commit()
        return RedirectResponse(f"/settings/emails/{key}?done=saved", status_code=303)
    if op == "test":
        problems += style_errors + errors
        notice = ""
        if not problems:
            notice, problems = _send_test(session, key, doc, style)
        return _page(request, session, key, doc, style, errors=problems, notice=notice, part=part)
    # Ops apply to the draft as posted (form ids match its positions), then
    # the result is cleaned like any save would be.
    if isinstance(raw.get("blocks"), list):
        open_at = _apply(op, tpl, raw, ids)
        doc, errors = T.clean(key, raw)
    else:
        open_at = None
    return _page(request, session, key, doc, style, open_at=open_at,
                 errors=problems + errors, part=part)


@router.post("/settings/emails/{key}/preview", response_class=HTMLResponse)
async def email_builder_preview(key: str, request: Request,
                                session: Session = Depends(get_session)):
    """The draft in the form as the preview page (the live preview's fetch).
    Uploads are ignored here; they are stored when the form is submitted."""
    tpl = _tpl(key)
    form = await request.form()
    raw, _ids, _ = await _draft(_NoFiles(form), tpl)
    doc, _errors = T.clean(key, raw)
    style, _ = _style_from_form(form)
    part = form.get("part") if form.get("part") in PARTS else "html"
    return HTMLResponse(email_brand.preview(session, key, email_brand.look(session), doc=doc,
                                            part=part, style=style),
                        headers={"Cache-Control": "no-store"})


@router.get("/settings/emails/{key}/preview", response_class=HTMLResponse)
def email_builder_saved_preview(key: str, request: Request,
                                session: Session = Depends(get_session)):
    _tpl(key)
    part = request.query_params.get("part")
    return HTMLResponse(email_brand.preview(session, key, email_brand.look(session),
                                            part=part if part in PARTS else ""),
                        headers={"Cache-Control": "no-store"})


@router.post("/settings/emails/{key}/reset")
def email_builder_reset(key: str, request: Request, session: Session = Depends(get_session)):
    _tpl(key)
    models.set_setting(session, T.setting_key(key), "")
    models.audit(session, request.state.user.id, "settings.email_template_reset", detail=key)
    session.commit()
    return RedirectResponse(f"/settings/emails/{key}?done=reset", status_code=303)


class _NoFiles:
    """A form view without file uploads (the live preview never stores files)."""

    def __init__(self, form):
        self._form = form

    def get(self, k, default=None):
        v = self._form.get(k, default)
        return default if hasattr(v, "filename") else v

    def getlist(self, k):
        return [v for v in self._form.getlist(k) if not hasattr(v, "filename")]

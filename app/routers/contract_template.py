import html as _html
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import contract_tokens, models
from app.company import company_info
from app.contract_defaults import (
    STALE_DAYS_KEY,
    STALE_DAYS_MAX,
    STALE_DAYS_MIN,
    TEMPLATE_KEY,
    parse_stale_days,
)
from app.db import get_session
from app.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

_COMPUTED = {"parties", "rates", "signature"}


def _blocks_from_form(form) -> list[dict]:
    """Rebuild the ordered block list from the editor form. `kind[]` gives block
    order. Text blocks carry a `title[]`/`body[]`/`when[]` (one each, in
    text-block order); computed blocks carry one `visible[]` value each
    ("on"/"off"), in their order. All consumed positionally as we walk `kind[]`.

    `when` round-trips through a hidden input: without it, saving the template
    silently stripped the spa section's includes_spa condition, making it
    render on every contract from then on."""
    kinds = form.getlist("kind")
    titles = form.getlist("title")
    bodies = form.getlist("body")
    whens = form.getlist("when")
    visibles = form.getlist("visible")
    # The editor UI always moves a block's inputs as one <section>, keeping
    # these parallel lists aligned — but a hand-crafted POST (or a future JS
    # regression) with mismatched counts would silently mis-pair titles with
    # the wrong bodies or drop content. Refuse rather than degrade.
    n_text = kinds.count("text")
    n_computed = sum(1 for k in kinds if k in _COMPUTED)
    # `when` may be absent entirely (older/handmade posts) — then every text
    # block is unconditional; if present it must align like the others.
    if not (len(titles) == len(bodies) == n_text
            and len(whens) in (0, n_text)
            and len(visibles) == n_computed):
        raise HTTPException(status_code=400, detail="form fields out of sync")
    vis_iter = iter(visibles)
    blocks = []
    ti = 0
    for kind in kinds:
        if kind == "text":
            blocks.append({"kind": "text",
                           "title": (titles[ti] if ti < len(titles) else ""),
                           "body": (bodies[ti] if ti < len(bodies) else ""),
                           "when": (whens[ti] if ti < len(whens) else "") or None})
            ti += 1
        elif kind in _COMPUTED:
            blocks.append({"kind": kind, "visible": next(vis_iter, "off") == "on"})
    return blocks


@router.post("/settings/contract/save")
async def save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    tpl = {"title": form.get("doc_title", "") or "Service Agreement",
           "blocks": _blocks_from_form(form)}
    models.set_setting(session, TEMPLATE_KEY, json.dumps(tpl))
    models.audit(session, request.state.user.id, "contract.template.save",
                 detail=f"{len(tpl['blocks'])} blocks")
    session.commit()
    return RedirectResponse("/settings#contract", status_code=303)


_SAMPLE_COMPANY = {"business_name": "Your Company", "company_address": "1 Main St",
                   "company_phone": "(000) 000-0000", "company_email": "you@example.com",
                   "company_license": "LIC-0000"}


def sample_objects(includes_spa: bool = True, company: dict | None = None):
    """The made-up contract the editor previews against. Real company details,
    where filled in, replace the placeholders so the preview reads like the
    owner's own paperwork; everything customer-side stays synthetic."""
    contract = SimpleNamespace(weekly_cents=8500, spa_cents=6000, open_close_cents=40000,
                               green_clean_cents=12000, storm_cents=9000, service_call_cents=15000,
                               weeks=32, includes_spa=includes_spa)
    pool = SimpleNamespace(label="Main pool", gallons=18432, water_type="salt", surface="plaster",
                           length_ft=16, width_ft=32, avg_depth_ft=5)
    customer = SimpleNamespace(name="Sample Customer", phone="(000) 000-0001",
                               email="customer@example.com")
    prop = SimpleNamespace(label="Home", address="123 Example Ave, Your City, FL 30000",
                           gate_code="0000", city="Your City", state="FL", zip="30000")
    merged = dict(_SAMPLE_COMPANY)
    merged.update({k: v for k, v in (company or {}).items() if k in merged and v})
    return contract, pool, prop, customer, merged


def sample_context(includes_spa: bool = True, company: dict | None = None) -> dict:
    contract, pool, prop, customer, merged = sample_objects(includes_spa, company)
    return contract_tokens.resolve_tokens(contract, pool, prop, customer, merged, tax_rate_ppm=0)


def _text_html(title: str, body: str, esc) -> str:
    """Same line rules as the PDF and the editor's page view: blank lines split
    paragraphs, consecutive "- " lines group into a bullet list."""
    out = [f'<h3 class="ct-h">{esc(title)}</h3>'] if title else []
    for i, para in enumerate(body.split("\n\n")):
        if i:
            out.append('<span class="ct-p gap"></span>')
        bullets = []
        for ln in (line for line in para.split("\n") if line.strip()):
            if ln.lstrip().startswith("- "):
                bullets.append(ln.lstrip()[2:])
                continue
            if bullets:
                out.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
                bullets = []
            out.append(f'<p class="ct-p">{esc(ln)}</p>')
        if bullets:
            out.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
    return "".join(out)


def _preview_html(tpl: dict, ctx: dict, sample=None) -> str:
    """The editor's sample preview: the template rendered the way a real
    contract renders it (spa-only sections dropped for a pool-only contract,
    built-in blocks filled from the sample objects)."""
    from app.contract_html import _parties_block, _rates_block

    def esc(s):
        return _html.escape(str(s))

    contract, _pool, prop, customer, company = sample or sample_objects()
    out = [f'<h2 class="ct-title">'
           f'{esc(contract_tokens.apply_tokens(tpl.get("title", ""), ctx))}</h2>']
    for block in tpl.get("blocks", []):
        kind = block.get("kind")
        if kind == "text":
            if block.get("when") == "includes_spa" and not contract.includes_spa:
                continue
            title = contract_tokens.apply_tokens(block.get("title", ""), ctx)
            body = contract_tokens.apply_tokens(block.get("body", ""), ctx)
            out.append(f'<div class="ce-sec">{_text_html(title, body, esc)}</div>')
        elif kind in _COMPUTED and block.get("visible", True):
            if kind == "parties":
                inner = _parties_block(prop, customer)
            elif kind == "rates":
                inner = _rates_block(contract, 0)
            else:
                inner = ('<div class="ce-sig"><div><span class="ce-line"></span><b>Client</b>'
                         f'<span>{esc(customer.name)}</span></div>'
                         '<div><span class="ce-line"></span>'
                         f'<b>{esc(company.get("business_name", ""))}</b>'
                         '<span>Authorised signature</span></div></div>')
            out.append(f'<div class="ce-sec">{inner}</div>')
    return "".join(out)


@router.post("/settings/contract/preview", response_class=HTMLResponse)
async def preview(request: Request, session: Session = Depends(get_session)):
    """Renders the posted (unsaved) editor form. `ct_sample_spa` picks whether
    the sample contract includes a spa; the save route ignores it."""
    form = await request.form()
    tpl = {"title": form.get("doc_title", ""), "blocks": _blocks_from_form(form)}
    sample = sample_objects(includes_spa=form.get("ct_sample_spa", "1") != "0",
                            company=company_info(session))
    contract, pool, prop, customer, company = sample
    ctx = contract_tokens.resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm=0)
    return HTMLResponse(_preview_html(tpl, ctx, sample))


@router.post("/settings/contract/rules")
async def save_rules(request: Request, session: Session = Depends(get_session)):
    """The contract follow-up rule: days before a sent, unsigned contract is
    flagged on the Contracts hub."""
    form = await request.form()
    days = parse_stale_days(form.get("stale_days", ""))
    if days is None:
        raise HTTPException(status_code=400,
                            detail=f"days must be a whole number from {STALE_DAYS_MIN} to {STALE_DAYS_MAX}")
    models.set_setting(session, STALE_DAYS_KEY, str(days))
    models.audit(session, request.state.user.id, "contract.rules.save",
                 detail=f"stale after {days} days")
    session.commit()
    return RedirectResponse("/settings#contract", status_code=303)

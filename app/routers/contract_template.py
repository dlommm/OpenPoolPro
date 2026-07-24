import html as _html
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import contract_tokens, models
from app.contract_defaults import TEMPLATE_KEY
from app.db import get_session
from app.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

_COMPUTED = {"parties", "rates", "signature"}


def _blocks_from_form(form) -> list[dict]:
    """Rebuild the ordered block list from the editor form. `kind[]` gives block
    order. Text blocks carry a `title[]`/`body[]` (one each, in text-block order);
    computed blocks carry one `visible[]` value each ("on"/"off"), in their order.
    Both are consumed positionally as we walk `kind[]`."""
    kinds = form.getlist("kind")
    titles = form.getlist("title")
    bodies = form.getlist("body")
    vis_iter = iter(form.getlist("visible"))
    blocks = []
    ti = 0
    for kind in kinds:
        if kind == "text":
            blocks.append({"kind": "text",
                           "title": (titles[ti] if ti < len(titles) else ""),
                           "body": (bodies[ti] if ti < len(bodies) else ""),
                           "when": None})
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


def sample_context() -> dict:
    contract = SimpleNamespace(weekly_cents=8500, spa_cents=6000, open_close_cents=40000,
                               green_clean_cents=12000, storm_cents=9000, service_call_cents=15000,
                               weeks=32, includes_spa=True)
    pool = SimpleNamespace(label="Main pool", gallons=18432, water_type="salt", surface="plaster",
                           length_ft=16, width_ft=32, avg_depth_ft=5)
    customer = SimpleNamespace(name="Sample Customer")
    prop = SimpleNamespace(label="Home", address="123 Example Ave, Your City, FL 30000",
                           gate_code="0000", city="Your City", state="FL", zip="30000")
    company = {"business_name": "Your Company", "company_address": "1 Main St",
               "company_phone": "(000) 000-0000", "company_email": "you@example.com",
               "company_license": "LIC-0000"}
    return contract_tokens.resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm=0)


def _preview_html(tpl: dict, ctx: dict) -> str:
    def esc(s):
        return _html.escape(str(s))

    out = [f'<h2 class="mb-2 text-center font-display text-lg font-bold">'
           f'{esc(contract_tokens.apply_tokens(tpl.get("title", ""), ctx))}</h2>']
    for block in tpl.get("blocks", []):
        kind = block.get("kind")
        if kind == "text":
            title = contract_tokens.apply_tokens(block.get("title", ""), ctx)
            body = contract_tokens.apply_tokens(block.get("body", ""), ctx)
            if title:
                out.append(f'<h3 class="mt-3 font-semibold text-ink">{esc(title)}</h3>')
            for para in body.split("\n\n"):
                lines = [ln for ln in para.split("\n") if ln.strip()]
                if lines and all(ln.lstrip().startswith("- ") for ln in lines):
                    out.append("<ul class='ml-4 list-disc'>" +
                               "".join(f"<li>{esc(ln.lstrip()[2:])}</li>" for ln in lines) + "</ul>")
                elif para.strip():
                    out.append(f'<p class="mt-1 text-sm">{esc(para)}</p>')
        elif kind in _COMPUTED and block.get("visible", True):
            out.append(f'<p class="mt-3 rounded border border-dashed border-seam '
                       f'px-2 py-1 text-xs uppercase tracking-wide text-mist">[{esc(kind)} block]</p>')
    return "".join(out)


@router.post("/settings/contract/preview", response_class=HTMLResponse)
async def preview(request: Request):
    form = await request.form()
    tpl = {"title": form.get("doc_title", ""), "blocks": _blocks_from_form(form)}
    return HTMLResponse(_preview_html(tpl, sample_context()))

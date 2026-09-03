"""Render a Contract as in-page HTML for the customer e-sign flow.

Mirrors app/contract_pdf.render_contract block-for-block (same template, same
token resolution, same rates math) so what the customer reads on the sign
page is a faithful rendering of the PDF they receive — but as real HTML:
scrollable on a phone, no iframe, styleable, and with the signature block
rendered as the sign-here anchor the page's form hooks into.

Same escape-everything-dynamic discipline as the PDF module: every value
that touches the output goes through html.escape.
"""
import html as _html

from app import contract_tokens, tax
from app.contract_defaults import DEFAULT_TEMPLATE, get_template


def _esc(value) -> str:
    return _html.escape(str(value)) if value is not None else ""


def _d(cents) -> str:
    try:
        return f"{(cents or 0) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _text_block(title: str, body: str) -> str:
    out = []
    if title:
        out.append(f'<h3 class="ct-h">{_esc(title)}</h3>')
    for para in (body or "").split("\n\n"):
        bullets = []
        for ln in (line for line in para.split("\n") if line.strip()):
            if ln.lstrip().startswith("- "):
                bullets.append(ln.lstrip()[2:])
            else:
                if bullets:
                    out.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
                    bullets = []
                out.append(f"<p>{_esc(ln)}</p>")
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
    return "".join(out)


def _parties_block(prop, customer) -> str:
    rows = [
        ("Service Address", getattr(prop, "address", "")),
        ("Customer Name", getattr(customer, "name", "")),
        ("Phone / Email", f"{getattr(customer, 'phone', '') or '—'} / "
                          f"{getattr(customer, 'email', '') or '—'}"),
    ]
    body = "".join(f"<p><b>{_esc(k)}:</b> {_esc(v)}</p>" for k, v in rows)
    return f'<div class="ct-parties">{body}</div>'


def _rates_block(contract, tax_rate_ppm: int) -> str:
    """Same figures and wording as contract_pdf._rates_table."""
    weekly = contract.weekly_cents or 0
    open_close = contract.open_close_cents or 0
    weeks = contract.weeks or 0
    season_total = weeks * weekly
    half_oc = open_close // 2 + (open_close % 2)

    if tax_rate_ppm:
        taxable = season_total + open_close
        total_tax = tax.tax_cents(taxable, tax_rate_ppm)
        rows = [
            ("Openings & Closings", f"${_d(open_close)} (${_d(half_oc)} each)"),
            (f"{weeks}-week pool service",
             f"{weeks} weeks x ${_d(weekly)}/week = ${_d(season_total)} (plus chemicals)"),
            ("Sales Tax", f"${_d(total_tax)}"),
            ("Total incl. tax", f"${_d(taxable + total_tax)}"),
        ]
    else:
        rows = [
            ("Openings & Closings", f"${_d(open_close)} plus tax (${_d(half_oc)} each)"),
            (f"{weeks}-week pool service",
             f"{weeks} weeks x ${_d(weekly)}/week = ${_d(season_total)} (plus tax & chemicals)"),
        ]
    rows.extend([
        ("Additional weeks",
         f"Any additional weeks of service over the {weeks} weeks as discussed above, "
         "will be billed accordingly as per the contract, plus chemicals."),
        ("Service Calls / Repairs", f"${_d(contract.service_call_cents)} per hour"),
        ("Green-to-Clean Service",
         "Pools requiring excessive cleaning due to algae or neglected conditions will be "
         f"billed at ${_d(contract.green_clean_cents)} per hour plus chemicals and materials."),
        ("Storm Cleanup",
         f"Storm, wind, or heavy debris cleanup will be billed at ${_d(contract.storm_cents)} "
         "per hour plus chemicals if required."),
    ])
    body = "".join(
        f'<tr><td class="ct-rl">{_esc(k)}</td><td>{_esc(v)}</td></tr>' for k, v in rows)
    return (f'<h3 class="ct-h">Service Rates</h3>'
            f'<table class="ct-rates"><tbody>{body}</tbody></table>')


def render_contract_html(contract, pool, prop, customer, company: dict,
                         tax_rate_ppm: int = 0, session=None) -> str:
    """The agreement's body HTML. The signature block renders as the
    <!--SIGN-HERE--> marker the sign page splits on to place its signature
    panel exactly where the document's signature lines sit — every other
    block matches the PDF's content."""
    company = company or {}
    template = get_template(session) if session is not None else DEFAULT_TEMPLATE
    ctx = contract_tokens.resolve_tokens(contract, pool, prop, customer, company,
                                         tax_rate_ppm)

    head = [f'<p class="ct-co"><b>{_esc(company.get("business_name"))}</b></p>']
    for key in ("company_address", "company_phone", "company_email", "company_license"):
        if company.get(key):
            head.append(f'<p class="ct-co">{_esc(company[key])}</p>')
    out = ["".join(head)]
    out.append(f'<h2 class="ct-title">'
               f'{_esc(contract_tokens.apply_tokens(template.get("title", ""), ctx))}</h2>')

    for block in template.get("blocks", []):
        kind = block.get("kind")
        if kind != "text" and block.get("visible") is False:
            continue
        if block.get("when") == "includes_spa" and not getattr(contract, "includes_spa", False):
            continue
        if kind == "text":
            out.append(_text_block(
                contract_tokens.apply_tokens(block.get("title", ""), ctx),
                contract_tokens.apply_tokens(block.get("body", ""), ctx)))
        elif kind == "parties":
            out.append(_parties_block(prop, customer))
        elif kind == "rates":
            out.append(_rates_block(contract, tax_rate_ppm))
        elif kind == "signature":
            out.append("<!--SIGN-HERE-->")
    return "".join(out)

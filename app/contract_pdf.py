"""Branded PDF contract renderer.

Turns a `Contract` (+ its `Pool`/`Property`/`Customer` and a company-info
dict) into "Pool & Spa Service Agreement" PDF bytes using reportlab
platypus (SimpleDocTemplate + Table/Paragraph). Mirrors app/invoice_pdf.py:
same styles pattern, same escape()-everything-dynamic discipline, same
"never raises" contract so a bad/blank contract still produces a usable
PDF instead of a 500.

All rate figures are read from the contract's frozen snapshot cents
(never recomputed) so a rendered contract always matches what was agreed
at signing time, even if company-wide rate tiers change later.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app import contract_tokens, tax
from app.contract_defaults import DEFAULT_TEMPLATE, get_template

_STYLES = getSampleStyleSheet()

_COMPANY_NAME_STYLE = ParagraphStyle(
    "ContractCompanyName", parent=_STYLES["Heading2"], spaceAfter=2,
)
_COMPANY_LINE_STYLE = ParagraphStyle(
    "ContractCompanyLine", parent=_STYLES["Normal"], fontSize=9, leading=12,
)
_TITLE_STYLE = ParagraphStyle(
    "ContractTitle", parent=_STYLES["Heading1"], alignment=1, spaceBefore=6, spaceAfter=10,
)
_BODY_STYLE = ParagraphStyle(
    "ContractBody", parent=_STYLES["Normal"], fontSize=10, leading=14, spaceAfter=8,
)
_SECTION_HEADER_STYLE = ParagraphStyle(
    "ContractSectionHeader", parent=_STYLES["Heading3"], spaceBefore=10, spaceAfter=4,
)
_META_STYLE = ParagraphStyle(
    "ContractMeta", parent=_STYLES["Normal"], fontSize=9, leading=13,
)
_BULLET_STYLE = ParagraphStyle(
    "ContractBullet", parent=_STYLES["Normal"], fontSize=10, leading=14,
    leftIndent=14, spaceAfter=2,
)
_SIGNATURE_STYLE = ParagraphStyle(
    "ContractSignature", parent=_STYLES["Normal"], fontSize=10, leading=22, spaceBefore=6,
)
_FOOTER_STYLE = ParagraphStyle(
    "ContractFooter", parent=_STYLES["Normal"], fontSize=8, textColor=colors.grey,
)


def _d(cents) -> str:
    """Format snapshot cents as a plain dollar figure, e.g. 2500 -> '25.00'."""
    try:
        return f"{(cents or 0) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _logo_flowable():
    """Best-effort company-logo Image flowable, or None on any failure.
    Delegates to the shared, alpha-flattening helper so the logo is never
    stretched (see app/pdf_logo.py)."""
    from app.pdf_logo import logo_flowable
    return logo_flowable()


def _safe(value) -> str:
    """escape() a dynamic value after coercing to str, tolerating None."""
    if value is None:
        return ""
    return escape(str(value))


def _header_table(company: dict) -> Table:
    company = company or {}
    logo = _logo_flowable() if company.get("has_logo") else None

    company_lines = [Paragraph(_safe(company.get("business_name")), _COMPANY_NAME_STYLE)]
    for value in (company.get("company_address"), company.get("company_phone"),
                  company.get("company_email"), company.get("company_license")):
        if value:
            company_lines.append(Paragraph(_safe(value), _COMPANY_LINE_STYLE))

    left_cell = [logo] if logo is not None else []
    left_cell.extend(company_lines)

    table = Table([[left_cell]], colWidths=[6.5 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _parties_table(prop, customer) -> Table:
    address = getattr(prop, "address", "") if prop is not None else ""
    name = getattr(customer, "name", "") if customer is not None else ""
    phone = getattr(customer, "phone", "") if customer is not None else ""
    email = getattr(customer, "email", "") if customer is not None else ""

    lines = [
        f"<b>Service Address:</b> {_safe(address)}",
        f"<b>Customer Name:</b> {_safe(name)}",
        f"<b>Phone / Email:</b> {_safe(phone)} / {_safe(email)}",
    ]
    para = Paragraph("<br/>".join(lines), _META_STYLE)
    table = Table([[para]], colWidths=[6.5 * inch])
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _rates_table(contract, company_name: str, tax_rate_ppm: int = 0) -> list:
    """Service Rates section flowables (title + one Table)."""
    weekly = getattr(contract, "weekly_cents", 0) or 0
    open_close = getattr(contract, "open_close_cents", 0) or 0
    weeks = getattr(contract, "weeks", 0) or 0
    service_call = getattr(contract, "service_call_cents", 0) or 0
    green_clean = getattr(contract, "green_clean_cents", 0) or 0
    storm = getattr(contract, "storm_cents", 0) or 0

    season_total_cents = weeks * weekly
    # Integer split, remainder on the first half: 2 x the displayed "each"
    # amounts must reconcile to the stated total even for an odd-cent total
    # (float division rendered '($61.73 each)' against a $123.45 total).
    half_open_close_cents = open_close // 2 + (open_close % 2)

    if tax_rate_ppm:
        # Tax the concrete fixed items: the weekly season total AND openings/closings.
        taxable_total_cents = season_total_cents + open_close
        total_tax_cents = tax.tax_cents(taxable_total_cents, tax_rate_ppm)
        total_incl_tax_cents = taxable_total_cents + total_tax_cents
        open_close_text = (
            f"${_d(open_close)} (${_d(half_open_close_cents)} each)"
        )
        weekly_line = (
            f"{weeks} weeks x ${_d(weekly)}/week = ${_d(season_total_cents)} "
            f"(plus chemicals)"
        )
    else:
        open_close_text = (
            f"${_d(open_close)} plus tax (${_d(half_open_close_cents)} each)"
        )
        weekly_line = (
            f"{weeks} weeks x ${_d(weekly)}/week = ${_d(season_total_cents)} "
            "(plus tax & chemicals)"
        )

    rows = [
        ["Openings & Closings", open_close_text],
        [f"{weeks}-week pool service", weekly_line],
    ]
    if tax_rate_ppm:
        rows.append(["Sales Tax", f"${_d(total_tax_cents)}"])
        rows.append(["Total incl. tax", f"${_d(total_incl_tax_cents)}"])
    rows.extend([
        ["Additional weeks",
         f"Any additional weeks of service over the {weeks} weeks as discussed above, "
         "will be billed accordingly as per the contract, plus chemicals."],
        ["Service Calls / Repairs", f"${_d(service_call)} per hour"],
        ["Green-to-Clean Service",
         "Pools requiring excessive cleaning due to algae or neglected conditions will be "
         f"billed at ${_d(green_clean)} per hour plus chemicals and materials."],
        ["Storm Cleanup",
         f"Storm, wind, or heavy debris cleanup will be billed at ${_d(storm)} per hour "
         "plus chemicals if required."],
    ])

    body = [[Paragraph(f"<b>{_safe(label)}</b>", _BODY_STYLE), Paragraph(_safe(text), _BODY_STYLE)]
            for label, text in rows]

    table = Table(body, colWidths=[2 * inch, 4.5 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
    ]))
    return [Paragraph("Service Rates", _SECTION_HEADER_STYLE), table]


def _text_flowables(title: str, body: str) -> list:
    """A resolved text block -> section header (if titled) + paragraphs/bullets.
    Paragraphs split on blank lines; any line starting with "- " is a bullet."""
    out = []
    if title:
        out.append(Paragraph(_safe(title), _SECTION_HEADER_STYLE))
    for para in (body or "").split("\n\n"):
        lines = [ln for ln in para.split("\n") if ln.strip()]
        for ln in lines:
            if ln.lstrip().startswith("- "):
                out.append(Paragraph(f"&#8226; {_safe(ln.lstrip()[2:])}", _BULLET_STYLE))
            else:
                out.append(Paragraph(_safe(ln), _BODY_STYLE))
    return out


def _signature_font() -> str:
    """Register the bundled script font (once) for the certificate page's
    signature rendering; fall back to Helvetica if anything's off."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        if "GreatVibes" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(
                TTFont("GreatVibes", "app/static/fonts/GreatVibes-Regular.ttf"))
        return "GreatVibes"
    except Exception:
        return "Helvetica"


def certificate_of_completion_pdf(*, contract_ref: str, signer_name: str,
                                  signer_email: str, signer_ip: str,
                                  sent_at_local: str, viewed_at_local: str,
                                  signed_at_local: str, document_hash: str,
                                  company_name: str, envelope_id: str) -> bytes:
    """The staff-side legal audit record (DocuSign's "Certificate of
    Completion"): the evidence that proves who signed what, when, from where,
    and that the signed file is unaltered. Never shown to the customer — their
    copy carries only the inline signature. Retained so the business can
    produce it if a signature is ever disputed.

    Raises on failure (an empty/partial audit record silently stored would be
    worse than surfacing the error)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                             topMargin=0.9 * inch, bottomMargin=0.9 * inch,
                             leftMargin=0.9 * inch, rightMargin=0.9 * inch)
    rows = [
        ("Agreement", contract_ref),
        ("Envelope ID", envelope_id),
        ("Signer", signer_name),
        ("Signer email", signer_email or "—"),
        ("Signer IP address", signer_ip or "—"),
        ("Sent", sent_at_local or "—"),
        ("First viewed", viewed_at_local or "—"),
        ("Signed", signed_at_local or "—"),
        ("Method", "Electronic signature via unique secure emailed link"),
        ("Document SHA-256", document_hash or "—"),
    ]
    detail = Table([[Paragraph(f"<b>{escape(k)}</b>", _META_STYLE),
                     Paragraph(escape(str(v)), _META_STYLE)] for k, v in rows],
                   colWidths=[1.7 * inch, 4.5 * inch])
    detail.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
    ]))
    doc.build([
        Paragraph("Certificate of Completion", _TITLE_STYLE),
        Spacer(1, 0.1 * inch),
        Paragraph(
            f"This record documents the electronic signing of the agreement below with "
            f"{escape(company_name or 'the company')}. The signer consented to do business "
            "electronically and to sign electronically under the U.S. ESIGN Act and UETA, "
            f"and adopted their signature by typing their name. <b>{escape(signer_name)}</b> "
            "affirmed that they read the agreement and agreed to its terms and conditions.",
            _BODY_STYLE),
        Spacer(1, 0.2 * inch),
        detail,
        Spacer(1, 0.2 * inch),
        Paragraph(
            "The Document SHA-256 above is a fingerprint of the signed PDF at the moment of "
            "signing. Re-computing the hash of the stored signed document and comparing it to "
            "this value proves the file has not been altered since it was signed.",
            _META_STYLE),
        Spacer(1, 0.3 * inch),
        Paragraph("Generated by OpenPoolPro", _FOOTER_STYLE),
    ])
    return buf.getvalue()


def render_contract(contract, pool, prop, customer, company: dict,
                     tax_rate_ppm: int = 0, session=None, signature=None) -> bytes:
    """Render one Contract to "Pool & Spa Service Agreement" PDF bytes.

    Never raises: any error assembling the story still yields best-effort
    bytes from an empty/partial document rather than propagating.

    signature: when None the signature block draws blank lines (the unsigned,
    approved copy). When a dict {"name", "date"} it draws the adopted
    signature INLINE in the signature block (script font) — the signed copy,
    DocuSign-style, with no separate certificate page and no IP shown to the
    customer. The audit trail (IP, timestamps, hash) lives staff-side in the
    Certificate of Completion.

    tax_rate_ppm defaults to 0 (tax off / not applicable), which renders
    identically to before this parameter existed: prose "plus tax &
    chemicals" only, no computed Sales Tax line. When truthy, a concrete
    "Sales Tax" figure and a "Season total incl. tax" row are added to the
    Service Rates table (see _rates_table).
    """
    buf = io.BytesIO()
    try:
        company = company or {}
        company_name = company.get("business_name") or ""
        template = get_template(session) if session is not None else DEFAULT_TEMPLATE
        ctx = contract_tokens.resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm)

        doc = SimpleDocTemplate(buf, pagesize=letter,
                                 topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                                 leftMargin=0.6 * inch, rightMargin=0.6 * inch)

        story = [_header_table(company), Spacer(1, 0.15 * inch)]
        story.append(Paragraph(
            _safe(contract_tokens.apply_tokens(template.get("title", ""), ctx)), _TITLE_STYLE))

        for block in template.get("blocks", []):
            kind = block.get("kind")
            if kind != "text" and block.get("visible") is False:
                continue
            if block.get("when") == "includes_spa" and not getattr(contract, "includes_spa", False):
                continue
            if kind == "text":
                title = contract_tokens.apply_tokens(block.get("title", ""), ctx)
                body = contract_tokens.apply_tokens(block.get("body", ""), ctx)
                story.extend(_text_flowables(title, body))
                story.append(Spacer(1, 0.06 * inch))
            elif kind == "parties":
                story.append(_parties_table(prop, customer))
                story.append(Spacer(1, 0.05 * inch))
            elif kind == "rates":
                story.extend(_rates_table(contract, company_name, tax_rate_ppm))
            elif kind == "signature":
                story.append(Spacer(1, 0.15 * inch))
                if signature:
                    sig_font = _signature_font()
                    story.append(Paragraph(
                        f'Client Signature: <font name="{sig_font}" size="22">'
                        f'{_safe(signature.get("name"))}</font>', _SIGNATURE_STYLE))
                    story.append(Paragraph(
                        f'Print Name: {_safe(signature.get("name"))} &nbsp;&nbsp; '
                        f'Date: {_safe(signature.get("date"))}', _SIGNATURE_STYLE))
                    story.append(Paragraph(
                        "Signed electronically under the U.S. ESIGN Act / UETA.",
                        _FOOTER_STYLE))
                else:
                    story.append(Paragraph(
                        "Client Signature: _______________________________ &nbsp;&nbsp; "
                        "Print Name: _______________________________ &nbsp;&nbsp; "
                        "Date: ___________", _SIGNATURE_STYLE))
                story.append(Paragraph(
                    f"{_safe(company_name)} Representative: "
                    "_______________________________ &nbsp;&nbsp; Date: ___________",
                    _SIGNATURE_STYLE))

        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph("Generated by OpenPoolPro", _FOOTER_STYLE))

        doc.build(story)
    except Exception:
        return buf.getvalue()
    return buf.getvalue()

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
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
    """Best-effort company-logo Image flowable, or None on any failure."""
    try:
        import os

        from app.routers.company import _logo_path

        path = _logo_path()
        if not os.path.isfile(path):
            return None
        img = Image(path)
        img.drawHeight = 0.75 * inch
        img.drawWidth = 0.75 * inch * (img.imageWidth / img.imageHeight)
        return img
    except Exception:
        return None


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


def _rates_table(contract, company_name: str) -> list:
    """Service Rates section flowables (title + one Table)."""
    weekly = getattr(contract, "weekly_cents", 0) or 0
    open_close = getattr(contract, "open_close_cents", 0) or 0
    weeks = getattr(contract, "weeks", 0) or 0
    service_call = getattr(contract, "service_call_cents", 0) or 0
    green_clean = getattr(contract, "green_clean_cents", 0) or 0
    storm = getattr(contract, "storm_cents", 0) or 0

    season_total_cents = weeks * weekly
    half_open_close_cents = open_close / 2

    rows = [
        ["Openings & Closings",
         f"${_d(open_close)} plus tax (${half_open_close_cents / 100:,.2f} each)"],
        [f"{weeks}-week pool service",
         f"{weeks} weeks x ${_d(weekly)}/week = ${_d(season_total_cents)} (plus tax & chemicals)"],
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
    ]

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


def render_contract(contract, pool, prop, customer, company: dict) -> bytes:
    """Render one Contract to "Pool & Spa Service Agreement" PDF bytes.

    Never raises: any error assembling the story still yields best-effort
    bytes from an empty/partial document rather than propagating.
    """
    buf = io.BytesIO()
    try:
        company = company or {}
        company_name = company.get("business_name") or ""
        pool_label = getattr(pool, "label", "") if pool is not None else ""

        doc = SimpleDocTemplate(buf, pagesize=letter,
                                 topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                                 leftMargin=0.6 * inch, rightMargin=0.6 * inch)

        story = [_header_table(company), Spacer(1, 0.15 * inch)]
        story.append(Paragraph("Pool &amp; Spa Service Agreement", _TITLE_STYLE))

        intro = (
            f"This Pool &amp; Spa Service Agreement (&#8220;Agreement&#8221;) is entered into "
            f"between {_safe(company_name)} (&#8220;Service Provider&#8221;) and the "
            f"undersigned Customer (&#8220;Client&#8221;) for swimming pool and/or spa "
            f"maintenance services at the property listed below, covering the "
            f"<b>{_safe(pool_label)}</b> pool."
        )
        story.append(Paragraph(intro, _BODY_STYLE))
        story.append(Spacer(1, 0.1 * inch))
        story.append(_parties_table(prop, customer))
        story.append(Spacer(1, 0.05 * inch))

        weekly = getattr(contract, "weekly_cents", 0) or 0
        scope_text = (
            f"{_safe(company_name)} will provide one (1) service visit per week on an "
            "assigned service day. Services may include: testing pool water chemistry, "
            "skimming and netting debris, vacuuming pool as needed, emptying skimmer and "
            "pump baskets, cleaning automatic pool cleaner, backwashing filter when "
            "necessary, brushing pool walls and tile when needed, inspecting filtration and "
            "circulation systems, adding necessary chemicals to maintain proper water "
            f"balance. All labor associated with these services is billed at a rate of "
            f"<b>${_d(weekly)}/week</b>, plus the cost of any chemicals, materials or "
            "additional services required."
        )
        story.append(Paragraph("Scope of Weekly Pool Service", _SECTION_HEADER_STYLE))
        story.append(Paragraph(scope_text, _BODY_STYLE))

        if getattr(contract, "includes_spa", False):
            spa = getattr(contract, "spa_cents", 0) or 0
            spa_text = (
                "Spa maintenance may include: vacuuming spa, cleaning strainer baskets and "
                "filter cartridges, testing water chemistry and adding necessary chemicals. "
                f"All labor associated with these services is billed at a rate of "
                f"<b>${_d(spa)}/hour</b>, plus the cost of any chemicals, materials or "
                "additional services required."
            )
            story.append(Paragraph("Spa Service", _SECTION_HEADER_STYLE))
            story.append(Paragraph(spa_text, _BODY_STYLE))

        story.extend(_rates_table(contract, company_name))

        story.append(Paragraph("Customer Requirements for Service", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            "To maintain safe and effective service, the Client agrees to the following:",
            _BODY_STYLE))
        for bullet in (
            f"All chemicals must be purchased through {_safe(company_name)}.",
            "Pools must use an automatic chlorinator, or salt system.",
            "Pool filtration must run approximately 12 hours per day via timer or automation.",
            "If pool access requires a gate key, lock code, or security access, it must be provided.",
            "Pets must be secured on the day of service.",
            "The pool water level must be maintained at normal operating level.",
        ):
            story.append(Paragraph(f"&#8226; {bullet}", _BULLET_STYLE))
        story.append(Paragraph(
            "Failure to meet these requirements may prevent service completion and may "
            "still result in a service charge.", _BODY_STYLE))

        story.append(Paragraph("Photo &amp; Marketing Release", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            f"Client grants {_safe(company_name)} permission to photograph and/or record "
            "the pool and surrounding areas for the purpose of documenting services and for "
            "marketing, advertising, and promotional use (including social media and "
            "website). No personal identifying information (such as address or client name) "
            "will be disclosed without prior written consent.", _BODY_STYLE))

        story.append(Paragraph("Payment Terms", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            "Payment for services, chemicals, and materials is due upon invoice unless "
            f"otherwise agreed in writing. {_safe(company_name)} reserves the right to "
            "suspend or terminate service for non-payment.", _BODY_STYLE))

        story.append(Paragraph("Equipment &amp; Liability Disclaimer", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            f"Client acknowledges that {_safe(company_name)} does not manufacture pool "
            "equipment and therefore cannot guarantee the performance of pumps, filters, "
            f"heaters, plumbing, liners, or other equipment. {_safe(company_name)} is not "
            "responsible for damage or service issues resulting from: equipment failure or "
            "aging components, improper installation by others, acts of nature (storm, "
            "flooding, lightning, debris, etc.), power outages or electrical issues, "
            "improper filtration operation or low water level. Any repairs required will be "
            "quoted separately.", _BODY_STYLE))

        story.append(Paragraph("Access &amp; Missed Service", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            "If service cannot be performed due to locked gates, pets, construction, unsafe "
            "conditions, or denied access, the visit may be skipped or billed as a service "
            "call. Service days may change due to weather, holidays, or operational "
            "scheduling.", _BODY_STYLE))

        story.append(Paragraph("Water Quality Disclaimer", _SECTION_HEADER_STYLE))
        story.append(Paragraph(
            f"While {_safe(company_name)} will make every reasonable effort to maintain "
            "proper water quality, clarity, and algae prevention, these conditions cannot be "
            "guaranteed in the event of equipment failure, adverse weather conditions, "
            "improper filtration, or customer interference.", _BODY_STYLE))

        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(
            "By signing below, the Client agrees to all terms and conditions of this "
            "service agreement.", _BODY_STYLE))
        story.append(Spacer(1, 0.15 * inch))
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

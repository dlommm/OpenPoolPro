"""Branded PDF invoice renderer.

Turns an `invoicing.InvoiceData` into invoice PDF bytes using reportlab
platypus (SimpleDocTemplate + Table/Paragraph). Defensive by design: an
invoice with no visits/chems/jobs, None visit durations, blank job
times_display, or a missing/corrupt company logo must still render a
usable PDF rather than raising.
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

from app import invoicing
from app.timefmt import to_local_date

_STYLES = getSampleStyleSheet()

_COMPANY_NAME_STYLE = ParagraphStyle(
    "CompanyName", parent=_STYLES["Heading2"], spaceAfter=2,
)
_COMPANY_LINE_STYLE = ParagraphStyle(
    "CompanyLine", parent=_STYLES["Normal"], fontSize=9, leading=12,
)
_INVOICE_TITLE_STYLE = ParagraphStyle(
    "InvoiceTitle", parent=_STYLES["Heading1"], alignment=2,  # right
)
_META_STYLE = ParagraphStyle(
    "Meta", parent=_STYLES["Normal"], fontSize=9, leading=13, alignment=2,
)
_SECTION_HEADER_STYLE = ParagraphStyle(
    "SectionHeader", parent=_STYLES["Heading3"], spaceBefore=10, spaceAfter=4,
)
_TABLE_HEADER_STYLE = ParagraphStyle(
    "TableHeader", parent=_STYLES["Normal"], fontSize=9, fontName="Helvetica-Bold",
)
_TABLE_CELL_STYLE = ParagraphStyle(
    "TableCell", parent=_STYLES["Normal"], fontSize=9,
)
_FOOTER_STYLE = ParagraphStyle(
    "Footer", parent=_STYLES["Normal"], fontSize=8, textColor=colors.grey,
)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _logo_flowable():
    """Best-effort company-logo Image flowable, or None on any failure."""
    try:
        from app.routers.company import _logo_path

        path = _logo_path()
        import os

        if not os.path.isfile(path):
            return None
        img = Image(path)
        img.drawHeight = 0.75 * inch
        img.drawWidth = 0.75 * inch * (img.imageWidth / img.imageHeight)
        return img
    except Exception:
        return None


def _header_table(data: invoicing.InvoiceData) -> Table:
    company = data.company or {}
    logo = _logo_flowable() if company.get("has_logo") else None

    company_lines = [Paragraph(escape(company.get("business_name") or ""), _COMPANY_NAME_STYLE)]
    for value in (company.get("company_address"), company.get("company_phone"),
                  company.get("company_email"), company.get("company_license")):
        if value:
            company_lines.append(Paragraph(escape(str(value)), _COMPANY_LINE_STYLE))

    left_cell = [logo] if logo is not None else []
    left_cell.extend(company_lines)

    right_cell = [Paragraph("INVOICE", _INVOICE_TITLE_STYLE)]

    table = Table([[left_cell, right_cell]], colWidths=[4 * inch, 2.5 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _meta_table(data: invoicing.InvoiceData) -> Table:
    lines = [f"<b>Invoice #:</b> {escape(data.number or '')}",
             f"<b>Period:</b> {escape(data.period or '')}"]
    if data.generated:
        lines.append(f"<b>Date:</b> {escape(to_local_date(data.generated))}")
    lines.append(f"<b>Bill to:</b> {escape(data.customer_name or '')}")
    if data.qb_name and data.qb_name != data.customer_name:
        lines.append(f"<b>QuickBooks name:</b> {escape(data.qb_name)}")
    if data.address:
        lines.append(escape(data.address))
    para = Paragraph("<br/>".join(lines), _META_STYLE)
    table = Table([[para]], colWidths=[6.5 * inch])
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _section(title: str, header: list[str], rows: list[list[str]],
             col_widths: list[float], section_total_cents: int | None = None):
    """One titled Table for a non-empty invoice section, or None if empty."""
    if not rows:
        return None

    flowables = [Paragraph(title, _SECTION_HEADER_STYLE)]

    body = [[Paragraph(h, _TABLE_HEADER_STYLE) for h in header]]
    for row in rows:
        body.append([Paragraph(escape(str(cell)), _TABLE_CELL_STYLE) for cell in row])

    style = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
    ]

    if section_total_cents is not None:
        blank_lead = [Paragraph("", _TABLE_CELL_STYLE) for _ in range(len(header) - 2)]
        body.append([*blank_lead,
                     Paragraph("<b>Subtotal</b>", _TABLE_CELL_STYLE),
                     Paragraph(f"<b>{_money(section_total_cents)}</b>", _TABLE_CELL_STYLE)])
        style.append(("LINEABOVE", (0, len(body) - 1), (-1, len(body) - 1), 0.5, colors.black))

    table = Table(body, colWidths=col_widths)
    table.setStyle(TableStyle(style))
    flowables.append(table)
    return flowables


def _visits_section(data: invoicing.InvoiceData):
    rows = []
    for v in data.visits:
        duration = f"{v.duration_minutes} min" if v.duration_minutes is not None else ""
        rows.append([v.date.strftime("%m/%d/%Y"), v.label or "", duration, _money(v.rate_cents)])
    total = sum(v.rate_cents for v in data.visits)
    return _section("Visits", ["Date", "Property", "Duration", "Amount"], rows,
                     [1.1 * inch, 2.6 * inch, 1.3 * inch, 1.5 * inch], total)


def _chems_section(data: invoicing.InvoiceData):
    rows = []
    for c in data.chems:
        rows.append([c.name or "", f"{c.quantity:g}", c.unit or "", _money(c.billable_cents)])
    total = sum(c.billable_cents for c in data.chems)
    return _section("Chemicals", ["Chemical", "Quantity", "Unit", "Amount"], rows,
                     [2.3 * inch, 1.2 * inch, 1.2 * inch, 1.8 * inch], total)


def _jobs_section(data: invoicing.InvoiceData):
    rows = []
    for j in data.jobs:
        rows.append([j.date.strftime("%m/%d/%Y"), j.title or "",
                     j.times_display or "", _money(j.labor_cents)])
    total = sum(j.labor_cents for j in data.jobs)
    return _section("Jobs", ["Date", "Job", "Time", "Amount"], rows,
                     [1.1 * inch, 2.6 * inch, 1.3 * inch, 1.5 * inch], total)


def _total_table(data: invoicing.InvoiceData) -> Table:
    table = Table([[Paragraph("<b>Total due</b>", _TABLE_HEADER_STYLE),
                    Paragraph(f"<b>{_money(data.total_cents)}</b>", _TABLE_HEADER_STYLE)]],
                   colWidths=[5 * inch, 1.5 * inch])
    table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
    ]))
    return table


def render_invoice(data: invoicing.InvoiceData) -> bytes:
    """Render one customer's invoice to PDF bytes. Never raises."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                             leftMargin=0.6 * inch, rightMargin=0.6 * inch)

    story = [_header_table(data), Spacer(1, 0.2 * inch), _meta_table(data), Spacer(1, 0.15 * inch)]

    for section in (_visits_section(data), _chems_section(data), _jobs_section(data)):
        if section:
            story.extend(section)

    story.append(Spacer(1, 0.15 * inch))
    story.append(_total_table(data))
    story.append(Spacer(1, 0.4 * inch))

    company = data.company or {}
    footer_lines = ["Thank you for your business.",
                    "Generated by OpenPoolPro"]
    license = company.get("company_license")
    if license:
        footer_lines.append(f"License #{escape(str(license))}")
    story.append(Paragraph("<br/>".join(footer_lines), _FOOTER_STYLE))

    doc.build(story)
    return buf.getvalue()

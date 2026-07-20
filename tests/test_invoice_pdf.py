import io
from datetime import date

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import invoicing, models
from app.invoice_pdf import render_invoice
from app.timefmt import to_local_date
from tests.test_analytics import _fix


def _text(pdf_bytes):
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def test_render_full_invoice(engine, client):
    _fix(engine)
    client.post("/settings/company", data={
        "business_name": "Coastline Pools LLC", "company_address": "1 Dock St",
        "company_phone": "555-0100", "company_email": "b@c.d",
        "company_license": "CPC-9"})
    with Session(engine) as s:
        alpha = s.scalars(select(models.Customer).where(
            models.Customer.name == "Alpha")).one()
        data = invoicing.invoice_data(s, alpha, 2026, 7)
        data.number = "INV-2026-0001"
    assert data.generated is not None
    text = _text(render_invoice(data))
    expected_date = to_local_date(data.generated)
    for expected in ["INV-2026-0001", "Coastline Pools LLC", "CPC-9", "Alpha",
                     "July 2026", "50.00", "12.00", "30.00", "92.00", expected_date]:
        assert expected in text, expected


def test_render_empty_sections_no_crash(engine):
    from app.db import Base
    from app import seeds
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        c = models.Customer(name="Emptyco")
        s.add(c)
        s.commit()
        data = invoicing.invoice_data(s, c, 2026, 7)
        data.number = "INV-2026-0002"
    text = _text(render_invoice(data))
    assert "Emptyco" in text and "0.00" in text


def _company(**overrides):
    company = {"business_name": "Coastline Pools LLC", "company_address": "",
               "company_phone": "", "company_email": "", "company_license": "",
               "has_logo": False}
    company.update(overrides)
    return company


def test_render_hostile_text_is_escaped_not_corrupted():
    """Free-text fields must not be interpreted as reportlab mini-XML markup.

    Regression for: unmatched '</b>' raising ValueError from paraparser, and
    '&'/'<'/'>' silently mangling or dropping text. All the hostile substrings
    below must survive render_invoice untouched and appear verbatim in the
    extracted PDF text (pypdf reads the literal glyphs drawn on the page, so
    escaping for the XML parser does not change what a reader sees).
    """
    data = invoicing.InvoiceData(
        company=_company(business_name="Bubbles < Co"),
        customer_name="AT&T Facilities <West>",
        qb_name="AT&T Facilities <West>",
        address="",
        number="INV-2026-0099",
        period="July 2026",
        visits=[],
        chems=[invoicing.ChemLine(name="Salt & Chlorine", quantity=2.0,
                                   unit="gal", billable_cents=500)],
        jobs=[invoicing.JobLine(title="Bad </b> close only", date=date(2026, 7, 1),
                                 times_display="", labor_cents=1000)],
        total_cents=1500,
    )
    pdf_bytes = render_invoice(data)  # must not raise
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    text = _text(pdf_bytes)
    # Verified against actual pypdf extraction: literal characters survive,
    # not "&amp;"/"&lt;" entities and not a parser crash.
    assert "AT&T Facilities" in text
    assert "<West>" in text
    assert "Bubbles < Co" in text
    assert "Salt & Chlorine" in text
    assert "Bad </b> close only" in text


def test_render_multi_row_section_subtotal():
    """A section with >=2 rows renders (no crash) and sums the subtotal
    correctly. Also pins the _section() off-by-one fix: previously the
    subtotal row had len(header)+1 cells, over-widening the table."""
    data = invoicing.InvoiceData(
        company=_company(),
        customer_name="Alpha",
        qb_name="Alpha",
        address="",
        number="INV-2026-0100",
        period="July 2026",
        visits=[
            invoicing.VisitLine(date=date(2026, 7, 1), label="Pool A",
                                 duration_minutes=60, rate_cents=5000),
            invoicing.VisitLine(date=date(2026, 7, 8), label="Pool B",
                                 duration_minutes=45, rate_cents=3000),
        ],
        chems=[], jobs=[], total_cents=8000,
    )
    pdf_bytes = render_invoice(data)
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    text = _text(pdf_bytes)
    assert "80.00" in text  # subtotal = 5000 + 3000 cents


def test_render_footer_matches_spec():
    """Footer must render its own "License #{license}" line, not just rely
    on the header's company-block license line. Fixture has both header and
    footer emit "CPC-9" (company block + footer), so a count-of-2 check
    can't false-pass if the footer line were silently dropped: only the
    header occurrence would remain and the count would be 1."""
    data = invoicing.InvoiceData(
        company=_company(company_license="CPC-9"),
        customer_name="Alpha",
        qb_name="Alpha",
        address="",
        number="INV-2026-0101",
        period="July 2026",
        visits=[], chems=[], jobs=[], total_cents=0,
    )
    text = _text(render_invoice(data))
    assert "Generated by Coastline Pools Planner" in text
    assert "License #CPC-9" in text
    assert text.count("CPC-9") == 2  # header company line + footer license line

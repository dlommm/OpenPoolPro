import io

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import invoicing, models
from app.invoice_pdf import render_invoice
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
    text = _text(render_invoice(data))
    for expected in ["INV-2026-0001", "Coastline Pools LLC", "CPC-9", "Alpha",
                     "July 2026", "50.00", "12.00", "30.00", "92.00"]:
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

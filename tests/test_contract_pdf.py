import io
from datetime import date
from types import SimpleNamespace

from pypdf import PdfReader

from app.contract_pdf import render_contract


def _text(pdf_bytes):
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def _company(**overrides):
    company = {"business_name": "OpenPoolPro LLC", "company_address": "1 Dock St",
               "company_phone": "555-0100", "company_email": "b@c.d",
               "company_license": "CPC-9", "has_logo": False}
    company.update(overrides)
    return company


def _contract(**overrides):
    fields = dict(
        weekly_cents=5000, open_close_cents=20000, green_clean_cents=7500,
        storm_cents=8500, service_call_cents=6500, spa_cents=4500,
        weeks=16, includes_spa=True, season_year=2026,
        open_date=date(2026, 4, 1), close_date=date(2026, 10, 15),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _pool(**overrides):
    fields = dict(label="Main pool")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _prop(**overrides):
    fields = dict(address="123 Lagoon Dr, Seaside, FL 32459")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _customer(**overrides):
    fields = dict(name="Jane Doe", phone="555-1234", email="jane@example.com")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_render_full_contract_with_spa():
    contract = _contract(includes_spa=True)
    pdf_bytes = render_contract(contract, _pool(), _prop(), _customer(), _company())
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    text = _text(pdf_bytes)

    # parties / property
    assert "Jane Doe" in text
    assert "123 Lagoon Dr" in text
    assert "Main pool" in text

    # dynamic rate figures (from snapshot cents)
    assert "50.00" in text  # weekly
    assert "65.00" in text  # service call
    assert "75.00" in text  # green-to-clean
    assert "85.00" in text  # storm
    assert "200.00" in text  # open/close total

    # weeks-week total = weeks * weekly = 16 * 5000 = 80000 cents = $800.00
    assert "16-week" in text
    assert "800.00" in text

    # spa clause present (the "Spa maintenance may include" body text is
    # unique to the conditional section; "Spa Service" alone also appears
    # in the fixed title "Pool & Spa Service Agreement")
    assert "Spa maintenance may include" in text
    assert "45.00" in text  # spa hourly rate


def test_render_contract_without_spa_omits_spa_section():
    contract = _contract(includes_spa=False)
    pdf_bytes = render_contract(contract, _pool(), _prop(), _customer(), _company())
    text = _text(pdf_bytes)
    assert "Spa maintenance may include" not in text
    # spa rate figure must not leak in either (it's only meaningful w/ the section)
    assert "45.00" not in text


def test_render_hostile_text_is_escaped_not_corrupted():
    """Free-text fields (pool label here) must not be interpreted as
    reportlab mini-XML markup. Mirrors test_invoice_pdf's hostile-text case:
    an unmatched '</b>' or raw '&'/'<'/'>' must survive escape() and appear
    verbatim in the extracted PDF text rather than crashing or vanishing."""
    contract = _contract()
    pool = _pool(label="Bad </b> pool & spa <West>")
    customer = _customer(name="AT&T Facilities <West>")
    prop = _prop(address="1 Main St <Unit B> & Rear")
    pdf_bytes = render_contract(contract, pool, prop, customer, _company())
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    text = _text(pdf_bytes)
    assert "AT&T Facilities" in text
    assert "<West>" in text
    assert "Bad </b> pool & spa" in text
    assert "1 Main St <Unit B> & Rear" in text


def test_render_never_raises_with_all_zero_rates():
    contract = _contract(
        weekly_cents=0, open_close_cents=0, green_clean_cents=0,
        storm_cents=0, service_call_cents=0, spa_cents=0, weeks=0,
        includes_spa=False,
    )
    pdf_bytes = render_contract(contract, _pool(), _prop(), _customer(), _company())
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    text = _text(pdf_bytes)
    assert "0.00" in text
    assert "0-week" in text


def test_render_never_raises_on_missing_related_objects():
    """None pool/prop/customer or an empty company dict must still produce
    best-effort bytes rather than propagating an exception."""
    contract = _contract()
    pdf_bytes = render_contract(contract, None, None, None, {})
    assert isinstance(pdf_bytes, bytes)

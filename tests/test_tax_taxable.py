from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models, seeds, tax
from app.db import Base


@pytest.fixture(autouse=True)
def _create_tables(engine):
    """The bare `engine` fixture returns an empty DB; create the schema so the
    tax_rates/settings tables exist for the DB-backed tests below."""
    Base.metadata.create_all(engine)


def _cat(session, labor="1", chems="1", weekly="1"):
    models.set_setting(session, "sales_tax_enabled", "1")
    models.set_setting(session, "tax_labor", labor)
    models.set_setting(session, "tax_chems", chems)
    models.set_setting(session, "tax_weekly", weekly)


def test_effective_labor_prefers_override():
    assert tax.effective_labor_cents(SimpleNamespace(rate_cents=5500, labor_override_cents=None)) == 5500
    assert tax.effective_labor_cents(SimpleNamespace(rate_cents=5500, labor_override_cents=7000)) == 7000
    assert tax.effective_labor_cents(SimpleNamespace(labor_cents=42500, labor_override_cents=None)) == 42500
    assert tax.effective_labor_cents(SimpleNamespace(labor_cents=42500, labor_override_cents=50000)) == 50000


def test_job_breakdown_taxes_labor(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s); _cat(s); s.commit()
        job = SimpleNamespace(labor_cents=18000, labor_override_cents=None,
                              tax_rate_ppm=66250, property=SimpleNamespace(state="NJ"))
        b = tax.job_breakdown(s, job)
        assert b["labor"] == 18000 and b["taxable"] == 18000
        assert b["tax"] == 1192 and b["total"] == 19192   # 18000 @ 6.625% = 1192.5 (banker's round -> 1192)


def test_job_breakdown_labor_toggle_off(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s); _cat(s, labor="0"); s.commit()
        job = SimpleNamespace(labor_cents=18000, labor_override_cents=None,
                              tax_rate_ppm=66250, property=SimpleNamespace(state="NJ"))
        b = tax.job_breakdown(s, job)
        assert b["taxable"] == 0 and b["tax"] == 0 and b["total"] == 18000

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models, seeds, tax
from app.db import Base


@pytest.fixture(autouse=True)
def _create_tables(engine):
    """Create all tables for tax tests."""
    Base.metadata.create_all(engine)


def _prop(state):
    return SimpleNamespace(property=SimpleNamespace(state=state))


def test_disabled_returns_zero(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s); s.commit()
        assert tax.tax_enabled(s) is False
        assert tax.resolve_rate_ppm(s, "NJ") == 0        # disabled -> 0


def test_resolve_from_state_when_enabled(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        models.set_setting(s, "sales_tax_enabled", "1"); s.commit()
        assert tax.resolve_rate_ppm(s, "NJ") == 66250
        assert tax.resolve_rate_ppm(s, "nj") == 66250    # case/space normalized
        assert tax.resolve_rate_ppm(s, " FL ") == 60000


def test_fallback_default_for_unknown_or_blank(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        models.set_setting(s, "sales_tax_enabled", "1")
        models.set_setting(s, "sales_tax_default_ppm", "50000"); s.commit()
        assert tax.resolve_rate_ppm(s, "") == 50000
        assert tax.resolve_rate_ppm(s, None) == 50000
        assert tax.resolve_rate_ppm(s, "ZZ") == 50000


def test_doc_tax_rate_prefers_frozen(engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        models.set_setting(s, "sales_tax_enabled", "1"); s.commit()
        frozen = SimpleNamespace(tax_rate_ppm=44444, property=SimpleNamespace(state="NJ"))
        live = SimpleNamespace(tax_rate_ppm=None, property=SimpleNamespace(state="NJ"))
        assert tax.doc_tax_rate_ppm(s, frozen) == 44444    # frozen wins
        assert tax.doc_tax_rate_ppm(s, live) == 66250      # NULL -> live resolve

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, seeds, tax_data
from app.db import Base


def test_state_seed_table_shape():
    d = tax_data.STATE_SALES_TAX
    assert len(d) == 51                      # 50 states + DC
    for z in ("AK", "DE", "MT", "NH", "OR"):
        assert d[z] == 0.0                   # no-sales-tax states
    assert d["NJ"] == 6.625


def test_seed_inserts_all_states(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        s.commit()
        rows = s.scalars(select(models.TaxRate)).all()
        assert len(rows) == 51
        nj = s.get(models.TaxRate, "NJ")
        assert nj.rate_ppm == 66250 and nj.overridden is False


def test_reseed_updates_unoverridden_preserves_overridden(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seeds.seed_tax_rates(s)
        s.commit()
        nj = s.get(models.TaxRate, "NJ")
        nj.rate_ppm = 99999
        nj.overridden = True                 # owner edit
        fl = s.get(models.TaxRate, "FL")
        fl.rate_ppm = 12345                   # stale non-overridden value
        s.commit()
    with Session(engine) as s:
        seeds.seed_tax_rates(s)               # simulates next app boot
        s.commit()
        assert s.get(models.TaxRate, "NJ").rate_ppm == 99999   # override preserved
        assert s.get(models.TaxRate, "FL").rate_ppm == 60000   # refreshed to seed

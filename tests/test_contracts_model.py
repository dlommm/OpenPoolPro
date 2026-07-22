from sqlalchemy.orm import Session

from app import models, rates
from app.db import Base


def _pool(s):
    c = models.Customer(name="Cust")
    p = models.Property(customer=c)
    s.add(p)
    s.commit()
    pool = models.Pool(property_id=p.id)
    s.add(pool)
    s.commit()
    return pool


def test_contract_defaults(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        pool = _pool(s)
        contract = models.Contract(pool_id=pool.id, season_year=2026)
        s.add(contract)
        s.commit()
        s.refresh(contract)
        assert contract.status == "draft"
        assert contract.weeks == 16
        assert contract.weekly_cents == 0
        assert contract.open_close_cents == 0
        assert contract.green_clean_cents == 0
        assert contract.storm_cents == 0
        assert contract.service_call_cents == 0
        assert contract.spa_cents == 0
        assert contract.includes_spa is False
        assert contract.pdf_filename == ""
        assert contract.signed_pdf_filename == ""
        assert contract.pool.id == pool.id


def test_contract_rates_for_matched_by_size(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.ContractRate(max_gallons=15000, label="Small",
                                   weekly_cents=1000, open_close_cents=2000,
                                   green_clean_cents=3000, storm_cents=4000,
                                   service_call_cents=5000, spa_cents=6000))
        s.add(models.ContractRate(max_gallons=30000, label="Medium",
                                   weekly_cents=1100, open_close_cents=2100,
                                   green_clean_cents=3100, storm_cents=4100,
                                   service_call_cents=5100, spa_cents=6100))
        s.commit()
        result = rates.contract_rates_for(s, 12000)
        assert result == {
            "weekly_cents": 1000,
            "open_close_cents": 2000,
            "green_clean_cents": 3000,
            "storm_cents": 4000,
            "service_call_cents": 5000,
            "spa_cents": 6000,
        }
        result = rates.contract_rates_for(s, 25000)
        assert result["weekly_cents"] == 1100


def test_contract_rates_for_catch_all(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.ContractRate(max_gallons=15000, label="Small",
                                   weekly_cents=1000, open_close_cents=2000,
                                   green_clean_cents=3000, storm_cents=4000,
                                   service_call_cents=5000, spa_cents=6000))
        s.add(models.ContractRate(max_gallons=None, label="Catch-all",
                                   weekly_cents=9000, open_close_cents=9100,
                                   green_clean_cents=9200, storm_cents=9300,
                                   service_call_cents=9400, spa_cents=9500))
        s.commit()
        result = rates.contract_rates_for(s, 99000)
        assert result["weekly_cents"] == 9000
        assert result["spa_cents"] == 9500


def test_contract_rates_for_no_match_returns_none(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.ContractRate(max_gallons=15000, label="Small",
                                   weekly_cents=1000, open_close_cents=2000,
                                   green_clean_cents=3000, storm_cents=4000,
                                   service_call_cents=5000, spa_cents=6000))
        s.commit()
        assert rates.contract_rates_for(s, 99000) is None


def test_contract_rates_for_empty_config_returns_none(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert rates.contract_rates_for(s, 12000) is None

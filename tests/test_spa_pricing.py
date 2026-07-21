from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, seeds
from app.db import Base


def _fresh(engine):
    Base.metadata.create_all(engine)
    seeds.run(engine)


def test_spa_tiers_seeded(engine):
    _fresh(engine)
    with Session(engine) as s:
        spa = s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).all()
        pool = s.scalars(select(models.RateTier).where(models.RateTier.kind == "pool")).all()
        assert len(spa) >= 1
        assert len(pool) >= 1
        assert all(t.kind == "spa" for t in spa)


def test_effective_rate_pool_only_regression(engine):
    _fresh(engine)
    with Session(engine) as s:
        c = models.Customer(name="Smith")
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=24000, kind="pool"))
        s.commit()
        from app.rates import effective_rate_cents, service_type
        assert effective_rate_cents(s, p) == 5500      # medium pool tier, unchanged
        assert service_type(p) == "pool"


def test_effective_rate_pool_plus_spa(engine):
    _fresh(engine)
    with Session(engine) as s:
        c = models.Customer(name="Jones")
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=24000, kind="pool"))   # 5500 pool tier
        s.add(models.Pool(property=p, gallons=500, kind="spa"))      # 2000 spa small tier
        s.commit()
        from app.rates import effective_rate_cents, service_type
        assert effective_rate_cents(s, p) == 7500      # 5500 + 2000
        assert service_type(p) == "pool_spa"


def test_effective_rate_spa_only_and_override(engine):
    _fresh(engine)
    with Session(engine) as s:
        c = models.Customer(name="Reed")
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=500, kind="spa"))
        s.commit()
        from app.rates import effective_rate_cents, service_type
        assert service_type(p) == "spa"
        assert effective_rate_cents(s, p) == 2000      # spa small tier only
        p.spa_rate_override_cents = 2500
        assert effective_rate_cents(s, p) == 2500      # spa override wins

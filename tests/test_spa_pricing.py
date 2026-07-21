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

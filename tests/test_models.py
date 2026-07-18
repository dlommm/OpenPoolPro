from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, seeds


def test_seeds_idempotent(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    seeds.run(engine)
    with Session(engine) as s:
        chems = s.scalars(select(models.Chemical)).all()
        assert len(chems) == 8
        tiers = s.scalars(select(models.RateTier)).all()
        assert len(tiers) == 3
        assert tiers[-1].max_gallons is None
        tasks = s.scalars(select(models.TaskTemplate)).all()
        assert len(tasks) == 8
        assert s.scalars(select(models.User)).one().role == "owner"


def test_settings_helpers(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "0"
        models.set_setting(s, "chem_markup_percent", "15")
        s.commit()
        assert models.get_setting(s, "chem_markup_percent", "0") == "15"


def test_utcnow_naive():
    from app.models import utcnow
    assert utcnow().tzinfo is None

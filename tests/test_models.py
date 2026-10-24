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
        assert len(tiers) == 5
        pool_tiers = [t for t in tiers if t.kind == "pool"]
        spa_tiers = [t for t in tiers if t.kind == "spa"]
        assert len(pool_tiers) == 3
        assert len(spa_tiers) == 2
        assert pool_tiers[-1].max_gallons is None
        tasks = s.scalars(select(models.TaskTemplate)).all()
        assert len(tasks) == 8
        assert s.scalars(select(models.User)).all() == []   # setup makes the owner


def test_seeds_backfills_spa_tiers_on_upgraded_install(engine):
    """An install that already has pool tiers but was seeded before spa tiers
    existed (or had them removed) must get spa tiers backfilled on the next
    seeds.run -- otherwise the spa component prices at $0 forever."""
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # Simulate a pre-spa-tier install: pool tiers present, no spa tiers,
        # so the top-level `if not any RateTier` seed guard is already false.
        for max_g, cents, label in seeds.TIERS:
            s.add(models.RateTier(max_gallons=max_g, rate_cents=cents, label=label, kind="pool"))
        s.commit()
        assert s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).first() is None

    seeds.run(engine)
    with Session(engine) as s:
        spa_tiers = s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).all()
        assert len(spa_tiers) == len(seeds.SPA_TIERS)
        labels = {t.label for t in spa_tiers}
        assert labels == {label for _, _, label in seeds.SPA_TIERS}

    # Idempotent: running again must not duplicate the backfilled spa tiers.
    seeds.run(engine)
    with Session(engine) as s:
        assert len(s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).all()) \
            == len(seeds.SPA_TIERS)


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


def test_seed_chemicals_get_roles(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    seeds.run(engine)  # idempotent
    with Session(engine) as s:
        by_name = {c.name: c for c in s.scalars(select(models.Chemical)).all()}
        assert by_name["Liquid chlorine"].role == "chlorine_liquid"
        assert by_name["Liquid chlorine"].strength_pct == 12.5
        assert by_name["Shock (cal-hypo)"].role == "chlorine_granular"
        assert by_name["Shock (cal-hypo)"].strength_pct == 65
        assert by_name["Muriatic acid"].role == "ph_down"
        assert abs(by_name["Muriatic acid"].strength_pct - 31.45) < 0.01
        assert by_name["Soda ash"].role == "ph_up"
        assert by_name["Baking soda"].role == "alkalinity_up"
        assert by_name["Stabilizer (CYA)"].role == "stabilizer"
        assert by_name["Salt"].role == "salt"
        assert by_name["Chlorine tabs 3in"].role == "chlorine_tab"

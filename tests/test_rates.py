import pytest
from sqlalchemy.orm import Session

from app import models, seeds
from app.rates import effective_rate_cents, gallons


def test_gallons_formulas():
    assert gallons("rectangle", 30, 15, 5) == round(30 * 15 * 5 * 7.5)   # 16875
    assert gallons("round", 20, None, 4) == round(10 * 10 * 3.14 * 4 * 7.5)  # 9420
    assert gallons("oval", 30, 15, 5) == round(30 * 15 * 5 * 5.9)
    assert gallons("kidney", 30, 15, 5) == round(30 * 15 * 5 * 6.0)


@pytest.mark.parametrize("dims", [(0, 10, 5), (10, -1, 5), (10, 10, 0), (101, 10, 5)])
def test_gallons_validation(dims):
    l, w, d = dims
    with pytest.raises(ValueError):
        gallons("rectangle", l, w, d)


def test_rate_resolution(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        c = models.Customer(name="Smith")
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=12000))
        s.add(models.Pool(property=p, gallons=24000))  # largest pool wins tier
        s.commit()
        assert effective_rate_cents(s, p) == 5500     # medium tier
        p.rate_override_cents = 6200
        assert effective_rate_cents(s, p) == 6200     # override wins
        big = models.Property(customer=c)
        s.add(models.Pool(property=big, gallons=90000))
        s.commit()
        assert effective_rate_cents(s, big) == 7000   # catch-all NULL tier

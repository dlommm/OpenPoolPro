from sqlalchemy.orm import Session

from app import models, rates
from app.db import Base


def _tmpl(**kw):
    t = models.JobTemplate(title="T", **kw)
    return t


def test_job_labor_cents():
    assert rates.job_labor_cents(18000, 2) == 36000
    assert rates.job_labor_cents(18000, 1.5) == 27000
    assert rates.job_labor_cents(0, 3) == 0


def test_template_hours_flat_when_no_matrix():
    t = _tmpl(est_hours=3.0)
    assert rates.template_hours(t, 12000) == (3.0, "flat")


def test_template_hours_matched_by_size():
    t = _tmpl(est_hours=1.0)
    t.prices = [models.JobTemplatePrice(max_gallons=15000, hours=2.0),
                models.JobTemplatePrice(max_gallons=30000, hours=3.0)]
    assert rates.template_hours(t, 12000) == (2.0, "matched")
    assert rates.template_hours(t, 25000) == (3.0, "matched")


def test_template_hours_catch_all_row():
    t = _tmpl(est_hours=1.0)
    t.prices = [models.JobTemplatePrice(max_gallons=15000, hours=2.0),
                models.JobTemplatePrice(max_gallons=None, hours=4.0)]
    assert rates.template_hours(t, 99000) == (4.0, "matched")


def test_template_hours_unmatched():
    t = _tmpl(est_hours=1.0)
    t.prices = [models.JobTemplatePrice(max_gallons=15000, hours=2.0)]
    assert rates.template_hours(t, 40000) == (None, "unmatched")


def test_default_labor_rate_cents(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert rates.default_labor_rate_cents(s) == 18000       # default
        models.set_setting(s, "labor_rate_cents", "20000"); s.commit()
        assert rates.default_labor_rate_cents(s) == 20000

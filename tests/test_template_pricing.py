from app import rates


class _Price:
    def __init__(self, g, cents):
        self.max_gallons, self.price_cents = g, cents


class _Tmpl:
    def __init__(self, prices):
        self.prices = prices


def test_template_price_resolution():
    assert rates.template_price_cents(_Tmpl([]), 20000) == (None, "flat")
    t = _Tmpl([_Price(15000, 15000), _Price(25000, 20000), _Price(None, 25000)])
    assert rates.template_price_cents(t, 12000) == (15000, "matched")
    assert rates.template_price_cents(t, 24000) == (20000, "matched")
    assert rates.template_price_cents(t, 99999) == (25000, "matched")  # catch-all
    no_catch = _Tmpl([_Price(15000, 15000)])
    assert rates.template_price_cents(no_catch, 99999)[1] == "unmatched"


def test_largest_pool_gallons():
    class Pool:
        def __init__(self, g, active=True, kind="pool"):
            self.gallons, self.active, self.kind = g, active, kind

    class Prop:
        pass

    p = Prop()
    p.pools = [Pool(10000), Pool(20000), Pool(9000, kind="spa"), Pool(30000, active=False)]
    assert rates.largest_pool_gallons(p) == 20000  # largest active pool-kind (spa/inactive ignored)


def test_save_template_hours(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    with Session(engine) as s:
        t = models.JobTemplate(title="Green clean", job_type="service", labor_cents=0)
        s.add(t); s.commit(); tid = t.id
    r = client.post(f"/settings/job_templates/{tid}/prices", data={
        "price_id": ["", ""], "label": ["Small", "Large"],
        "max_gallons": ["15000", ""], "hours": ["1.5", "2.5"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.JobTemplatePrice)
                         .where(models.JobTemplatePrice.job_template_id == tid)).all()
        by = {p.label: p for p in rows}
        assert by["Small"].max_gallons == 15000 and by["Small"].hours == 1.5
        assert by["Large"].max_gallons is None and by["Large"].hours == 2.5


def test_save_template_hours_blank_deletes_row(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    with Session(engine) as s:
        t = models.JobTemplate(title="Green clean", job_type="service", labor_cents=0)
        t.prices = [models.JobTemplatePrice(label="Small", max_gallons=15000, hours=1.5)]
        s.add(t); s.commit(); tid, pid = t.id, t.prices[0].id
    r = client.post(f"/settings/job_templates/{tid}/prices", data={
        "price_id": [str(pid)], "label": ["Small"],
        "max_gallons": ["15000"], "hours": [""]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.JobTemplatePrice)
                         .where(models.JobTemplatePrice.job_template_id == tid)).all()
        assert rows == []


def test_save_default_labor_rate(client, engine):
    from sqlalchemy.orm import Session
    from app import models
    r = client.post("/settings/labor_rate", data={"labor_rate_dollars": "225.00"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "labor_rate_cents", "0") == "22500"


def test_save_default_labor_rate_ignores_garbage(client, engine):
    from sqlalchemy.orm import Session
    from app import models
    # seed a known value first
    client.post("/settings/labor_rate", data={"labor_rate_dollars": "150"},
                follow_redirects=False)
    r = client.post("/settings/labor_rate", data={"labor_rate_dollars": "not-a-number"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "labor_rate_cents", "0") == "15000"  # unchanged


def test_save_template_est_hours(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    r = client.post("/settings/job_templates", data={
        "template_id": [""], "title": ["Filter clean"],
        "description": ["Clean the filter"], "job_type": ["service"],
        "labor_dollars": ["0"], "est_hours": ["2.5"],
        "tasks_text": [""]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        t = s.scalars(select(models.JobTemplate)
                      .where(models.JobTemplate.title == "Filter clean")).one()
        assert t.est_hours == 2.5


def test_save_template_est_hours_defaults_to_one_on_blank(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    r = client.post("/settings/job_templates", data={
        "template_id": [""], "title": ["No hours given"],
        "description": [""], "job_type": ["service"],
        "labor_dollars": ["0"], "est_hours": [""],
        "tasks_text": [""]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        t = s.scalars(select(models.JobTemplate)
                      .where(models.JobTemplate.title == "No hours given")).one()
        assert t.est_hours == 1.0


def _seed_prop_with_pool(engine, gallons, name):
    from sqlalchemy.orm import Session
    from app import models
    with Session(engine) as s:
        c = models.Customer(name=name)
        p = models.Property(customer=c)
        s.add(p)
        s.add(models.Pool(property=p, gallons=gallons, kind="pool"))
        s.commit()
        return p.id


def _template_with_small_price(engine):
    from sqlalchemy.orm import Session
    from app import models
    with Session(engine) as s:
        t = models.JobTemplate(title="Green clean", job_type="service", labor_cents=0)
        t.prices = [models.JobTemplatePrice(label="Small", max_gallons=15000, price_cents=15000)]
        s.add(t); s.commit()
        return t.id


def test_create_job_auto_prices_from_template(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    pid = _seed_prop_with_pool(engine, 12000, "Match")
    tid = _template_with_small_price(engine)
    r = client.post("/jobs/new", data={"property_id": pid, "title": "Clean",
                                        "job_type": "service", "template_id": str(tid)},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Clean")).one()
        assert j.labor_cents == 15000  # matched 12k <= 15k tier -> $150


def test_create_job_blocked_when_unmatched(client, engine):
    pid = _seed_prop_with_pool(engine, 40000, "TooBig")
    tid = _template_with_small_price(engine)
    r = client.post("/jobs/new", data={"property_id": pid, "title": "BigClean",
                                        "job_type": "service", "template_id": str(tid)},
                    follow_redirects=False)
    assert r.status_code == 400  # 40k matches no size row and no price entered -> blocked
    # entering a price lets it through
    r2 = client.post("/jobs/new", data={"property_id": pid, "title": "BigClean2",
                                         "job_type": "service", "template_id": str(tid),
                                         "labor_dollars": "300"}, follow_redirects=False)
    assert r2.status_code == 303


def test_template_prices_page_renders(client, engine):
    tid = _template_with_small_price(engine)
    html = client.get(f"/settings/job_templates/{tid}/prices").text
    assert "Hours by pool size" in html and "Add common size" in html

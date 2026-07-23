"""Task 11: pay-at-job collect price on the job detail page.

The completer (INCLUDING a tech, who normally sees no money) must see the
final customer PRICE to collect on this job -- service + tax. Jobs carry no
chemicals, so this is simpler than the visit version (Task 10): PRICE only,
never cost, never markup.
"""
from sqlalchemy.orm import Session

from app import models, seeds


def _seed_taxed_job(engine, client):
    with Session(engine) as s:
        c = models.Customer(name="JobCo")
        p = models.Property(customer=c, label="Home", state="NJ")
        s.add(p)
        s.commit()
        pid = p.id
        models.set_setting(s, "sales_tax_enabled", "1")
        seeds.seed_tax_rates(s)
        s.commit()
    r = client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Filter clean", "description": "",
        "priority": "normal", "rate_dollars": "180", "hours": "1", "scheduled_date": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        job = s.query(models.Job).order_by(models.Job.id.desc()).first()
        assert job.labor_cents == 18000
        return job.id


def test_tech_sees_collect_price_not_cost(client, tech_client, engine):
    jid = _seed_taxed_job(engine, client)
    t = tech_client(name="fieldtech")
    html = t.get(f"/jobs/{jid}").text
    assert "Collect" in html
    assert "$180.00" in html           # service price, visible to tech
    assert "$11.92" in html            # tax: 18000 @ 6.625% -> banker's rounding
    assert "$191.92" in html           # total to collect
    assert "cost" not in html.lower()  # cost/markup never exposed

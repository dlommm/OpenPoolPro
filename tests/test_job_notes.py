"""Tech-facing NOTES on the job detail page.

Notes are NOT money -- every authenticated user (incl. a bare tech) may add
or update them at any time, not just at completion. Weekly-visit notes
(app/templates/visits/form.html) are a separate, unrelated field and are not
touched here.
"""
from sqlalchemy.orm import Session

from app import models


def _seed_job(engine, client):
    with Session(engine) as s:
        c = models.Customer(name="NoteCo")
        p = models.Property(customer=c, label="Home", state="NJ")
        s.add(p)
        s.commit()
        pid = p.id
    r = client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Filter clean", "description": "",
        "priority": "normal", "rate_dollars": "", "hours": "", "scheduled_date": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        job = s.query(models.Job).order_by(models.Job.id.desc()).first()
        return job.id


def test_tech_can_add_and_see_job_notes(client, tech_client, engine):
    jid = _seed_job(engine, client)
    t = tech_client(name="fieldtech")

    r = t.post(f"/jobs/{jid}/notes", data={"notes": "gate code is 1234"},
              follow_redirects=False)
    assert r.status_code == 303
    assert r.status_code != 403

    with Session(engine) as s:
        job = s.get(models.Job, jid)
        assert job.notes == "gate code is 1234"

    html = t.get(f"/jobs/{jid}").text
    assert "gate code is 1234" in html

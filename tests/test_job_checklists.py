from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def test_job_template_tasks_replace_on_save(client, engine):
    r = client.post("/settings/job_templates", data={
        "template_id": [""], "title": ["Green pool cleanup"],
        "description": ["Shock + clarify"], "job_type": ["service"],
        "labor_dollars": ["150"],
        "tasks_text": ["Test water\nAdd shock\nBrush walls\n"]},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        t = s.scalars(select(models.JobTemplate).where(
            models.JobTemplate.title == "Green pool cleanup")).one()
        labels = [x.label for x in sorted(t.tasks, key=lambda z: z.position)]
        assert labels == ["Test water", "Add shock", "Brush walls"]

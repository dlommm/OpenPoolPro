from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed_route(engine, today):
    with Session(engine) as s:
        c = models.Customer(name="RouteGuy")
        p = models.Property(customer=c, label="Home", address="9 Palm St",
                            frequency="weekly", weekday=today.weekday(),
                            anchor_date=today - timedelta(days=28), route_order=1)
        s.add(p)
        j = models.Job(property=p, title="Fix light", status="scheduled",
                       scheduled_date=today)
        s.add(j)
        s.commit()
        return p.id


def test_today_lists_stops_and_jobs(client, engine):
    today = date.today()
    seed_route(engine, today)
    html = client.get("/").text
    assert "RouteGuy" in html
    assert "9 Palm St" in html
    assert "Fix light" in html
    assert "0 of 1 done" in html


def test_today_progress_counts_done(client, engine):
    today = date.today()
    pid = seed_route(engine, today)
    with Session(engine) as s:
        s.add(models.Visit(property_id=pid, date=today, status="done", rate_cents=4500))
        s.commit()
    assert "1 of 1 done" in client.get("/").text

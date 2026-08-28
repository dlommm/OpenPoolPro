from datetime import date, timedelta

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
    assert "Weekly service" in html
    assert "Service / Jobs today" in html


def test_today_has_google_maps_fallback_link(client, engine):
    # M2 pin: alongside the Apple Maps link, a secondary Google Maps link
    # must be present for techs whose default maps app isn't Apple's.
    seed_route(engine, date.today())
    html = client.get("/").text
    assert "maps.google.com" in html


def test_today_progress_counts_done(client, engine):
    today = date.today()
    pid = seed_route(engine, today)
    with Session(engine) as s:
        s.add(models.Visit(property_id=pid, date=today, status="done", rate_cents=4500))
        s.commit()
    assert "1 of 1 done" in client.get("/").text


# ---------------------------------------------------------------------------
# One-time "backup code used" banner (?backup_remaining=N), mirroring the
# analytics "applied" banner pattern: tolerant int parse, no banner on junk.
# ---------------------------------------------------------------------------

def test_backup_remaining_banner_shows_with_valid_int(client):
    r = client.get("/?backup_remaining=3")
    assert r.status_code == 200
    assert "Backup code used" in r.text
    assert "3 remaining" in r.text


def test_backup_remaining_banner_absent_with_garbage_param(client):
    r = client.get("/?backup_remaining=garbage")
    assert r.status_code == 200
    assert "Backup code used" not in r.text


def test_backup_remaining_banner_absent_without_param(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Backup code used" not in r.text

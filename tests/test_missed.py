from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, models


def test_missed_shows_and_dismisses(client, engine):
    # Use the same "today" the Today route computes (business timezone), not
    # date.today() — near-midnight UTC the two can disagree and flake the test.
    today = datetime.now(config.tz()).date()
    last_week = today - timedelta(days=7)
    with Session(engine) as s:
        c = models.Customer(name="MissedGuy")
        p = models.Property(customer=c, frequency="weekly",
                            weekday=last_week.weekday(), anchor_date=last_week)
        s.add(p)
        s.commit()
        pid = p.id
        # A weekly property matches its weekday every 7 days, so missed_stops'
        # 14-day lookback always contains a second occurrence at today-14 (in
        # addition to last_week at today-7). Mark that older cycle "done" so
        # it doesn't roll forward and reappear once last_week is dismissed —
        # this test is only about the single last_week miss, not the
        # roll-forward-to-an-older-unmet-stop behavior (already covered by
        # tests/test_scheduling.py::test_missed).
        s.add(models.Visit(property_id=pid, date=today - timedelta(days=14), status="done"))
        s.commit()
    html = client.get("/").text
    assert "Missed</h2>" in html and "MissedGuy" in html
    assert "Dismiss" in html
    client.post(f"/visit/{pid}/{last_week}/dismiss")
    html = client.get("/").text
    # Careful: last_week.weekday() == today.weekday() always (a 7-day offset
    # never changes the weekday), so the stop is due today too — that's fine,
    # "MissedGuy" is expected to still appear in the regular stops list.
    # But "Missed" is a literal substring of "MissedGuy", so a bare
    # `"Missed" not in html` check would trivially fail (self-contradicting)
    # whenever the property is still due today — check for the section
    # heading markup instead of the bare word.
    assert "Missed</h2>" not in html
    with Session(engine) as s:
        v = s.scalars(select(models.Visit).where(models.Visit.date == last_week)).one()
        assert v.status == "skipped" and v.resolved is True

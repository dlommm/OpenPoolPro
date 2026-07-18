from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed(engine, weekday=0):
    with Session(engine) as s:
        c = models.Customer(name="W")
        p = models.Property(customer=c, frequency="weekly", weekday=weekday,
                            anchor_date=date(2026, 7, 6), route_order=0)
        s.add(p)
        s.commit()
        return p.id


def monday(d=None):
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def test_week_renders_seven_days(client, engine):
    seed(engine)
    html = client.get(f"/week?start={monday()}").text
    for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        assert day in html
    assert "W" in html


def test_move_once(client, engine):
    pid = seed(engine)
    mon = monday()
    tue = mon + timedelta(days=1)
    client.post("/week/move", data={"property_id": str(pid), "from_date": str(mon),
                                    "to_date": str(tue), "mode": "once"})
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        assert v.date == tue and v.original_date == mon and v.status == "due"
    with Session(engine) as s:  # weekday unchanged
        assert s.get(models.Property, pid).weekday == 0


def test_move_forever(client, engine):
    pid = seed(engine)
    mon = monday()
    wed = mon + timedelta(days=2)
    client.post("/week/move", data={"property_id": str(pid), "from_date": str(mon),
                                    "to_date": str(wed), "mode": "forever"})
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        assert p.weekday == wed.weekday() and p.anchor_date == wed


def test_reorder(client, engine):
    a = seed(engine)
    with Session(engine) as s:
        c = models.Customer(name="W2")
        p = models.Property(customer=c, frequency="weekly", weekday=0,
                            anchor_date=date(2026, 7, 6), route_order=1)
        s.add(p)
        s.commit()
        b = p.id
    client.post("/week/reorder", data={"property_id": str(b), "direction": "up"})
    with Session(engine) as s:
        assert s.get(models.Property, b).route_order == 0
        assert s.get(models.Property, a).route_order == 1


def test_move_once_dedupes_existing_visit(client, engine):
    pid = seed(engine)
    mon = monday()
    tue = mon + timedelta(days=1)
    data = {"property_id": str(pid), "from_date": str(mon),
            "to_date": str(tue), "mode": "once"}
    client.post("/week/move", data=data)
    client.post("/week/move", data=data)
    with Session(engine) as s:
        assert len(s.scalars(select(models.Visit)).all()) == 1


def test_reorder_clamps_at_edges(client, engine):
    a = seed(engine)
    for direction in ["up", "down"]:
        client.post("/week/reorder", data={"property_id": str(a), "direction": direction})
        with Session(engine) as s:
            assert s.get(models.Property, a).route_order == 0


def test_move_missing_property_404(client, engine):
    mon = monday()
    r = client.post("/week/move", data={"property_id": "999", "from_date": str(mon),
                                        "to_date": str(mon), "mode": "once"})
    assert r.status_code == 404

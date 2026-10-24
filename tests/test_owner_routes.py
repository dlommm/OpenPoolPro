"""Owner routes (design v0.13): an owner can hold a route, but only by hand.
/routes/assign and the planner accept an active owner explicitly; the
balancer never moves stops onto or off one and shows their column read-only."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.scheduling import balance_moves, weekday_load


def _user(engine, name, role="tech", active=True):
    from app.auth_core import hash_password
    with Session(engine) as s:
        u = models.User(name=name, username=name.lower().replace(" ", ""), role=role,
                        active=active, password_hash=hash_password("techpass-1"))
        s.add(u)
        s.commit()
        return u.id


def _owner_id(engine):
    with Session(engine) as s:
        return s.scalars(select(models.User).where(models.User.username == "owner")).one().id


def _prop(engine, name, weekday, tech_id=None, route_order=0, active=True):
    with Session(engine) as s:
        p = models.Property(customer=models.Customer(name=name), frequency="weekly",
                            weekday=weekday, anchor_date=date(2026, 1, 5),
                            assigned_tech_id=tech_id, route_order=route_order, active=active)
        s.add(p)
        s.commit()
        return p.id


def _assignee(engine, pid):
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        return p.assigned_tech_id, p.route_order


def test_assign_to_active_owner_joins_end_of_their_route(client, engine):
    owner = _owner_id(engine)
    _prop(engine, "Held A", 2, owner, route_order=4)
    pid = _prop(engine, "Loose", 2, None)
    r = client.post("/routes/assign", data={"property_id": pid, "tech_id": owner},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/routes/balance"
    assert _assignee(engine, pid) == (owner, 5)
    with Session(engine) as s:
        log = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "route.assign")).one()
        assert "by hand" in log.detail


def test_assign_to_second_admin_owner(client, engine):
    partner = _user(engine, "Alex Morgan", role="admin")
    pid = _prop(engine, "Partner stop", 0)
    client.post("/routes/assign", data={"property_id": pid, "tech_id": partner})
    assert _assignee(engine, pid)[0] == partner


def test_assign_rejects_inactive_owner_inactive_tech_and_unknown_ids(client, engine):
    gone_owner = _user(engine, "Gone Owner", role="admin", active=False)
    gone_tech = _user(engine, "Gone Tech", active=False)
    pid = _prop(engine, "Stay", 3)
    for uid in (gone_owner, gone_tech):
        r = client.post("/routes/assign", data={"property_id": pid, "tech_id": uid})
        assert r.status_code == 400
    assert client.post("/routes/assign",
                       data={"property_id": pid, "tech_id": 99999}).status_code == 404
    assert client.post("/routes/assign",
                       data={"property_id": 99999, "tech_id": _owner_id(engine)}).status_code == 404
    off = _prop(engine, "Inactive", 3, active=False)
    assert client.post("/routes/assign",
                       data={"property_id": off, "tech_id": _owner_id(engine)}).status_code == 400
    assert _assignee(engine, pid)[0] is None and _assignee(engine, off)[0] is None


def test_assign_still_needs_view_all_routes(engine, client, tech_client):
    pid = _prop(engine, "Perm", 0)
    t = tech_client(name="noroutes")
    r = t.post("/routes/assign", data={"property_id": pid, "tech_id": _owner_id(engine)},
               follow_redirects=False)
    assert r.status_code == 403
    assert _assignee(engine, pid)[0] is None


def test_balance_moves_never_touches_or_targets_owner(client, engine):
    owner = _owner_id(engine)
    sam = _user(engine, "Sam Reyes")
    lee = _user(engine, "Lee Park")
    held = [_prop(engine, f"Owner{i}", 1, owner, route_order=i) for i in range(6)]
    for i in range(4):
        _prop(engine, f"Sam{i}", 1, sam, route_order=i)
    loose = [_prop(engine, f"Loose{i}", 1, None, route_order=10 + i) for i in range(2)]
    with Session(engine) as s:
        moves = balance_moves(weekday_load(s)[1], [sam, lee])
        assert all(dest in (sam, lee) for _, dest in moves)
        assert not any(p.id in held for p, _ in moves)
    client.post("/routes/apply")
    assert all(_assignee(engine, pid)[0] == owner for pid in held)
    assert all(_assignee(engine, pid)[0] in (sam, lee) for pid in loose)
    with Session(engine) as s:
        on_owner = s.scalars(select(models.Property).where(
            models.Property.assigned_tech_id == owner)).all()
        assert sorted(p.id for p in on_owner) == sorted(held)


def test_balance_with_only_owner_never_assigns_to_owner(client, engine):
    owner = _owner_id(engine)
    _prop(engine, "Mine", 4, owner)
    pid = _prop(engine, "Loose", 4)
    r = client.post("/routes/apply", follow_redirects=False)
    assert "applied=0" in r.headers["location"]
    assert _assignee(engine, pid)[0] is None
    html = client.get("/routes/balance").text
    # No suggested technician, but the owner can still be chosen by hand.
    assert "Choose a person" in html and "Owner · by hand" in html


def test_balance_page_shows_owner_column_read_only(client, engine):
    owner = _owner_id(engine)
    _user(engine, "Sam Reyes")
    for i in range(3):
        _prop(engine, f"Held{i}", 0, owner, route_order=i)
    _prop(engine, "Loose", 0)
    html = client.get("/routes/balance?preview=1").text
    assert "Owner · by hand" in html
    assert '<span class="rb-cell muted">3</span>' in html
    # The suggestion is a technician; the owner is offered as "someone else".
    assert "Assign to Sam Reyes" in html
    assert "Someone else" in html
    # The preview never moves the owner's stops onto Sam.
    assert "Held0" not in html.split("Even out the routes")[1]


def test_balance_page_hides_owner_column_without_owner_stops(client, engine):
    _user(engine, "Sam Reyes")
    _prop(engine, "SamStop", 0, None)
    assert "· by hand</span></th>" not in client.get("/routes/balance").text


def test_planner_lists_owners_after_technicians_and_moves_stop(client, engine):
    owner = _owner_id(engine)
    sam = _user(engine, "Zed Tech")  # alphabetically after "Owner"; still listed first
    pid = _prop(engine, "Hand me", 5, sam)
    _prop(engine, "Owner first", 5, owner, route_order=0)
    html = client.get(f"/routes/plan?weekday=5&tech_id={sam}").text
    assert html.index("Zed Tech") < html.index("<b>Owner")
    assert "1 stop · set by hand" in html
    assert "Move a stop to another route" in html
    r = client.post("/routes/plan/assign", data={"property_id": pid, "tech_id": owner,
                                                 "from_tech_id": str(sam)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/routes/plan?weekday=5&tech_id={sam}&assigned={owner}"
    assert _assignee(engine, pid) == (owner, 1)
    page = client.get(r.headers["location"]).text
    assert "Stop moved to the end of Owner" in page and "set by hand." in page
    owner_view = client.get(f"/routes/plan?weekday=5&tech_id={owner}").text
    assert "Hand me" in owner_view and "by hand</span>" in owner_view


def test_planner_assign_redirect_ignores_garbage_source(client, engine):
    pid = _prop(engine, "X", 1)
    r = client.post("/routes/plan/assign", data={"property_id": pid, "tech_id": _owner_id(engine),
                                                 "from_tech_id": "//evil"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/routes/plan?weekday=1&tech_id=0&")
    gone = _user(engine, "Gone Owner", role="admin", active=False)
    assert client.post("/routes/plan/assign",
                       data={"property_id": pid, "tech_id": gone}).status_code == 400


def test_planner_assign_needs_view_all_routes(engine, client, tech_client):
    pid = _prop(engine, "Perm", 0)
    t = tech_client(name="noplan")
    r = t.post("/routes/plan/assign", data={"property_id": pid, "tech_id": _owner_id(engine)},
               follow_redirects=False)
    assert r.status_code == 403


def test_owner_route_on_today_crew_week_and_sheet(client, engine):
    owner = _owner_id(engine)
    sam = _user(engine, "Sam Reyes")
    from app.timefmt import business_today
    today = business_today()
    _prop(engine, "Sam today", today.weekday(), sam)
    _prop(engine, "Owner today", today.weekday(), owner)
    crew = client.get("/?crew=1").text
    assert "Owner today" in crew and "Sam today" in crew
    assert client.get(f"/?tech={owner}").status_code == 200
    assert "Owner (" in client.get("/week").text
    sheet = client.get(f"/today/print?tech={owner}").text
    assert "Owner today" in sheet and "Sam today" not in sheet

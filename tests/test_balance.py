from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.scheduling import balance_moves, weekday_load


def _tech(engine, name, active=True):
    from app.auth_core import hash_password
    with Session(engine) as s:
        u = models.User(name=name.title(), username=name, role="tech", active=active,
                        password_hash=hash_password("techpass-1"))
        s.add(u)
        s.commit()
        return u.id


def _prop(engine, name, weekday, tech_id=None, active=True, frequency="weekly",
          customer_active=True, route_order=0):
    with Session(engine) as s:
        c = models.Customer(name=name, active=customer_active)
        p = models.Property(customer=c, frequency=frequency, weekday=weekday,
                            anchor_date=date(2026, 1, 5), assigned_tech_id=tech_id,
                            active=active, route_order=route_order)
        s.add(p)
        s.commit()
        return p.id


def test_weekday_load_groups_and_filters(client, engine):
    sam = _tech(engine, "sam")
    lee = _tech(engine, "lee")
    _prop(engine, "A", 0, sam)
    _prop(engine, "B", 0, lee)
    _prop(engine, "C", 0, None)                     # unassigned
    _prop(engine, "D", 0, sam, active=False)          # inactive property excluded
    _prop(engine, "E", 0, sam, frequency="none")      # frequency none excluded
    _prop(engine, "F", 0, sam, customer_active=False)  # inactive customer excluded
    _prop(engine, "G", 1, lee)                        # different weekday
    with Session(engine) as s:
        load = weekday_load(s)
        assert set(load.keys()) == {0, 1}
        assert set(load[0].keys()) == {sam, lee, None}
        assert len(load[0][sam]) == 1
        assert len(load[0][lee]) == 1
        assert len(load[0][None]) == 1
        assert len(load[1][lee]) == 1


def test_golden_greedy_5_1_to_3_3(client, engine):
    sam = _tech(engine, "samg")
    lee = _tech(engine, "leeg")
    for i in range(5):
        _prop(engine, f"A{i}", 0, sam, route_order=i)
    _prop(engine, "B0", 0, lee, route_order=0)
    with Session(engine) as s:
        load = weekday_load(s)
        moves = balance_moves(load[0], [sam, lee])
    assert len(moves) == 2
    assert all(dest == lee for _, dest in moves)
    # the two moved are sam's highest-route_order properties (A4, A3)
    assert {p.customer.name for p, _ in moves} == {"A4", "A3"}
    counts = {sam: 5, lee: 1}
    for prop, dest in moves:
        counts[prop.assigned_tech_id] -= 1
        counts[dest] += 1
    assert counts == {sam: 3, lee: 3}


def test_unassigned_distributed_to_lowest_count_first(client, engine):
    sam = _tech(engine, "sam3")
    lee = _tech(engine, "lee3")
    lo, hi = (sam, lee) if sam < lee else (lee, sam)
    _prop(engine, "S0", 3, lo, route_order=0)
    _prop(engine, "S1", 3, lo, route_order=1)
    for i in range(3):
        _prop(engine, f"U{i}", 3, None, route_order=i)
    with Session(engine) as s:
        load = weekday_load(s)
        moves = balance_moves(load[3], sorted([sam, lee]))
    by_name = {p.customer.name: dest for p, dest in moves}
    assert len(moves) == 3
    assert by_name["U0"] == hi   # hi starts at 0 (lowest) -> picked first
    assert by_name["U1"] == hi   # hi=1 vs lo=2 -> hi still lower
    assert by_name["U2"] == lo   # tie at 2-2 -> ascending tech id wins -> lo


def test_balance_moves_pure_and_empty_when_no_techs():
    assert balance_moves({}, []) == []


def test_balance_single_tech_absorbs_all_unassigned(client, engine):
    sam = _tech(engine, "samsingle")
    for i in range(3):
        _prop(engine, f"U{i}", 0, None, route_order=i)
    with Session(engine) as s:
        load = weekday_load(s)
        moves = balance_moves(load[0], [sam])
    assert len(moves) == 3
    assert all(dest == sam for _, dest in moves)


def test_balance_all_unassigned_even_split_ascending_id_ties(client, engine):
    sam = _tech(engine, "samsplit")
    lee = _tech(engine, "leesplit")
    lo, hi = sorted([sam, lee])
    for i in range(4):
        _prop(engine, f"V{i}", 1, None, route_order=i)
    with Session(engine) as s:
        load = weekday_load(s)
        moves = balance_moves(load[1], sorted([sam, lee]))
    assert len(moves) == 4
    counts = {sam: 0, lee: 0}
    for _, dest in moves:
        counts[dest] += 1
    assert counts[sam] == 2 and counts[lee] == 2
    # zero pre-load: every tie is broken by ascending tech id
    by_route = sorted(moves, key=lambda m: m[0].route_order)
    assert by_route[0][1] == lo   # tie 0-0 -> lo
    assert by_route[1][1] == hi   # lo=1, hi=0 -> hi
    assert by_route[2][1] == lo   # tie 1-1 -> lo
    assert by_route[3][1] == hi   # lo=2, hi=1 -> hi


def test_balance_already_balanced_three_three_zero_moves(client, engine):
    sam = _tech(engine, "sambal")
    lee = _tech(engine, "leebal")
    for i in range(3):
        _prop(engine, f"S{i}", 2, sam, route_order=i)
    for i in range(3):
        _prop(engine, f"L{i}", 2, lee, route_order=i)
    with Session(engine) as s:
        load = weekday_load(s)
        moves = balance_moves(load[2], sorted([sam, lee]))
    assert moves == []


def test_preview_deterministic_and_apply_matches(client, engine):
    sam = _tech(engine, "sam2")
    lee = _tech(engine, "lee2")
    for i in range(5):
        _prop(engine, f"X{i}", 2, sam, route_order=i)
    _prop(engine, "Y0", 2, lee, route_order=0)
    r1 = client.get("/routes/balance?preview=1").text
    r2 = client.get("/routes/balance?preview=1").text
    assert r1 == r2

    with Session(engine) as s:
        load = weekday_load(s)
        preview_moves = balance_moves(load[2], sorted([sam, lee]))
        expected = {(p.id, dest) for p, dest in preview_moves}
    assert len(expected) == 2

    client.post("/routes/apply")
    with Session(engine) as s:
        for pid, dest in expected:
            assert s.get(models.Property, pid).assigned_tech_id == dest


def test_apply_persists_and_banner(client, engine):
    sam = _tech(engine, "samB")
    lee = _tech(engine, "leeB")
    for i in range(5):
        _prop(engine, f"P{i}", 4, sam, route_order=i)
    _prop(engine, "Q0", 4, lee, route_order=0)
    r = client.post("/routes/apply", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/routes/balance")
    page = client.get(r.headers["location"]).text
    assert "2 properties reassigned" in page
    with Session(engine) as s:
        counts = Counter(p.assigned_tech_id for p in
                         s.scalars(select(models.Property)).all())
        assert counts[sam] == 3 and counts[lee] == 3


def test_apply_with_no_moves_needed_says_zero(client, engine):
    sam = _tech(engine, "samZ")
    _prop(engine, "OnlyOne", 6, sam)
    r = client.post("/routes/apply", follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert "0 properties reassigned" in page


def test_assign_route_validates_active(client, engine):
    sam = _tech(engine, "samC")
    inactive_tech = _tech(engine, "deadtech", active=False)
    pid = _prop(engine, "Free", 5, None, route_order=0)
    inactive_pid = _prop(engine, "Gone", 5, None, route_order=1, active=False)

    client.post("/routes/assign", data={"property_id": pid, "tech_id": inactive_tech})
    with Session(engine) as s:
        assert s.get(models.Property, pid).assigned_tech_id is None

    client.post("/routes/assign", data={"property_id": inactive_pid, "tech_id": sam})
    with Session(engine) as s:
        assert s.get(models.Property, inactive_pid).assigned_tech_id is None

    r = client.post("/routes/assign", data={"property_id": pid, "tech_id": sam},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Property, pid).assigned_tech_id == sam

    r = client.post("/routes/assign", data={"property_id": 99999, "tech_id": sam})
    assert r.status_code == 404


def test_matrix_page_renders_counts_and_assign_button(client, engine):
    sam = _tech(engine, "samD")
    _prop(engine, "Mine", 0, sam)
    _prop(engine, "Loose", 0, None)
    html = client.get("/routes/balance").text
    assert "Route balance" in html
    assert "Loose" in html
    assert "Assign to" in html


def test_zero_techs_page_renders_without_buttons(client, engine):
    html = client.get("/routes/balance").text
    assert "Route balance" in html
    assert "Preview balance" not in html
    assert "Apply balance" not in html
    assert "Assign to" not in html


def test_week_page_links_to_balance_only_when_techs_visible(client, engine, tech_client):
    _tech(engine, "samW")
    admin_html = client.get("/week").text
    assert '/routes/balance' in admin_html
    t = tech_client(name="bareweek")
    tech_html = t.get("/week").text
    assert '/routes/balance' not in tech_html


def test_permission_sweep_403(client, tech_client, engine):
    sam = _tech(engine, "samE")
    pid = _prop(engine, "Perm", 0, None)
    t = tech_client(name="bareperm")
    assert t.get("/routes/balance", follow_redirects=False).status_code == 403
    assert t.post("/routes/assign", data={"property_id": pid, "tech_id": sam},
                  follow_redirects=False).status_code == 403
    assert t.post("/routes/apply", follow_redirects=False).status_code == 403

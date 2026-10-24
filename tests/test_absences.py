from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.scheduling import absence_map, effective_tech_id, missed_stops, stops_for_date


def _tech(engine, name, **flags):
    from app.auth_core import hash_password
    with Session(engine) as s:
        u = models.User(name=name.title(), username=name, role="tech", active=True,
                        password_hash=hash_password("techpass-1"), **flags)
        s.add(u)
        s.commit()
        return u.id


def _prop(engine, name, weekday, tech_id=None, anchor=None):
    with Session(engine) as s:
        c = models.Customer(name=name)
        # Default anchor = today, so today's occurrence is the property's first
        # ever — otherwise last week's occurrence lands in the missed-stops
        # section, where a today-only absence correctly does NOT apply (per-day
        # maps), and page-level "name not in page" assertions trip on the
        # missed row. Unit tests that query a fixed past date pass anchor=d.
        p = models.Property(customer=c, frequency="weekly", weekday=weekday,
                            anchor_date=anchor or date.today(), assigned_tech_id=tech_id)
        s.add(p)
        s.commit()
        return p.id


def _absence(engine, user_id, start, end, cover_id=None, note=""):
    with Session(engine) as s:
        a = models.TechAbsence(user_id=user_id, start_date=start, end_date=end,
                               cover_tech_id=cover_id, note=note)
        s.add(a)
        s.commit()
        return a.id


def _uid(engine, username):
    with Session(engine) as s:
        return s.scalars(select(models.User).where(models.User.username == username)).one().id


# ---- pure resolution -------------------------------------------------------

def test_effective_tech_id_pure_resolution():
    assert effective_tech_id(None, {}) is None
    assert effective_tech_id(5, {}) == 5
    assert effective_tech_id(5, {5: 7}) == 7
    assert effective_tech_id(5, {5: None}) is None  # absent, no cover
    assert effective_tech_id(5, {5: 7, 7: None}) is None  # cover also absent


# ---- absence_map boundary --------------------------------------------------

def test_absence_map_boundary_days(client, engine):
    sam = _tech(engine, "sam")
    start, end = date(2026, 7, 10), date(2026, 7, 12)
    _absence(engine, sam, start, end)
    with Session(engine) as s:
        assert sam in absence_map(s, start)  # d == start: active
        assert sam in absence_map(s, end)    # d == end: active
        assert sam not in absence_map(s, end + timedelta(days=1))
        assert sam not in absence_map(s, start - timedelta(days=1))


def test_absence_map_overlapping_last_created_wins(client, engine):
    # Two overlapping absences for the same user, different covers: the
    # later-created row (higher id) must win deterministically.
    sam = _tech(engine, "samord")
    lee = _tech(engine, "leeord")
    pat = _tech(engine, "patord")
    d = date(2026, 7, 15)
    _absence(engine, sam, d, d, cover_id=lee)
    _absence(engine, sam, d, d, cover_id=pat)  # created later -> higher id
    with Session(engine) as s:
        m = absence_map(s, d)
    assert m[sam] == pat


def test_disabled_cover_tech_resolves_to_unassigned(client, engine):
    # absence_map must only return an active user as a cover; a cover that
    # is later disabled resolves to None (unassigned), matching visible_to's
    # disabled-tech-as-unassigned philosophy — not a dangling id.
    sam = _tech(engine, "samdisc")
    lee = _tech(engine, "leedisc")
    d = date(2026, 7, 16)
    _absence(engine, sam, d, d, cover_id=lee)
    with Session(engine) as s:
        s.get(models.User, lee).active = False
        s.commit()
    with Session(engine) as s:
        m = absence_map(s, d)
    assert m[sam] is None


# ---- stops_for_date effective_tech_id --------------------------------------

def test_stops_for_date_resolves_cover(client, engine):
    sam = _tech(engine, "sam1")
    lee = _tech(engine, "lee1")
    d = date(2026, 7, 13)
    _prop(engine, "CoverProp", d.weekday(), sam, anchor=d)
    _absence(engine, sam, d, d, cover_id=lee)
    with Session(engine) as s:
        stops = stops_for_date(s, d)
        assert len(stops) == 1
        assert stops[0].effective_tech_id == lee


def test_stops_for_date_absent_no_cover_is_none(client, engine):
    sam = _tech(engine, "sam2")
    d = date(2026, 7, 13)
    _prop(engine, "NoCoverProp", d.weekday(), sam, anchor=d)
    _absence(engine, sam, d, d, cover_id=None)
    with Session(engine) as s:
        stops = stops_for_date(s, d)
        assert stops[0].effective_tech_id is None


def test_stops_for_date_cover_also_absent_is_none(client, engine):
    sam = _tech(engine, "sam3")
    lee = _tech(engine, "lee3")
    d = date(2026, 7, 13)
    _prop(engine, "DoubleAbsentProp", d.weekday(), sam, anchor=d)
    _absence(engine, sam, d, d, cover_id=lee)
    _absence(engine, lee, d, d, cover_id=None)
    with Session(engine) as s:
        stops = stops_for_date(s, d)
        assert stops[0].effective_tech_id is None


def test_stops_for_date_unaffected_without_absence(client, engine):
    sam = _tech(engine, "sam4")
    d = date(2026, 7, 13)
    _prop(engine, "PlainProp", d.weekday(), sam, anchor=d)
    with Session(engine) as s:
        stops = stops_for_date(s, d)
        assert stops[0].effective_tech_id == sam


def test_missed_stops_effective_tech_id(client, engine):
    today = date.today()
    past = today - timedelta(days=3)
    sam = _tech(engine, "sam5")
    lee = _tech(engine, "lee5")
    _prop(engine, "MissedCovered", past.weekday(), sam, anchor=past)
    _absence(engine, sam, past, past, cover_id=lee)
    with Session(engine) as s:
        missed = missed_stops(s, today)
    assert len(missed) == 1
    assert missed[0].effective_tech_id == lee


# ---- visibility / HTTP integration -----------------------------------------

def test_covering_tech_sees_covered_stop_in_today_list(client, engine, tech_client):
    wd = date.today().weekday()
    sam = _tech(engine, "sam6")
    lee_client = tech_client(name="lee6")
    lee = _uid(engine, "lee6")
    _prop(engine, "CoveredStop", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=lee)
    assert "CoveredStop" in lee_client.get("/").text


def test_absent_tech_own_list_excludes_covered_stop(client, engine, tech_client):
    wd = date.today().weekday()
    sam_client = tech_client(name="sam7")
    sam = _uid(engine, "sam7")
    lee = _tech(engine, "lee7")
    _prop(engine, "AbsentMineStop", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=lee)
    assert "AbsentMineStop" not in sam_client.get("/").text


def test_absent_no_cover_visible_to_everyone(client, engine, tech_client):
    """Pins the exact unassigned-stop visibility semantics: an absent tech
    with no cover resolves to effective_tech_id None, which visible_to
    treats as visible to every tech (matching existing unassigned behavior),
    not just admins."""
    wd = date.today().weekday()
    sam_client = tech_client(name="sam8")
    sam = _uid(engine, "sam8")
    other_client = tech_client(name="other8")
    _prop(engine, "NoCoverStop", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=None)
    assert "NoCoverStop" in other_client.get("/").text
    assert "NoCoverStop" in sam_client.get("/").text
    assert "NoCoverStop" in client.get("/").text


def test_admin_tech_filter_matches_cover(client, engine):
    wd = date.today().weekday()
    sam = _tech(engine, "sam9")
    lee = _tech(engine, "lee9")
    _prop(engine, "FilterCovered", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=lee)
    assert "FilterCovered" in client.get(f"/?tech={lee}").text
    assert "FilterCovered" not in client.get(f"/?tech={sam}").text


def test_week_visibility_and_filter_match_cover(client, engine, tech_client):
    today = date.today()
    start = today - timedelta(days=today.weekday())
    sam = _tech(engine, "sam10")
    lee_client = tech_client(name="lee10")
    lee = _uid(engine, "lee10")
    _prop(engine, "WeekCovered", today.weekday(), sam)
    _absence(engine, sam, today, today, cover_id=lee)
    assert "WeekCovered" in lee_client.get("/week").text
    admin_lee = client.get(f"/week?start={start}&tech={lee}").text
    admin_sam = client.get(f"/week?start={start}&tech={sam}").text
    assert "WeekCovered" in admin_lee
    assert "WeekCovered" not in admin_sam


# ---- badges / chips ---------------------------------------------------------

def test_covering_chip_has_tag_safety_and_title(client, engine):
    wd = date.today().weekday()
    sam = _tech(engine, "sam11")
    lee = _tech(engine, "lee11")
    _prop(engine, "ChipStop", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=lee)
    html = client.get("/").text
    assert 'title="covering"' in html
    assert 'pill p-warn" title="covering"' in html


def test_week_chip_renders_covering(client, engine):
    today = date.today()
    start = today - timedelta(days=today.weekday())
    sam = _tech(engine, "sam12")
    lee = _tech(engine, "lee12")
    _prop(engine, "WeekChipStop", today.weekday(), sam)
    _absence(engine, sam, today, today, cover_id=lee)
    html = client.get(f"/week?start={start}").text
    assert 'title="covering"' in html


def test_week_chip_renders_plain_tech_initials(client, engine):
    # No absence involved: the plain assigned-tech initials chip branch
    # (tag ml-1, no tag-safety, no title="covering").
    today = date.today()
    start = today - timedelta(days=today.weekday())
    sam = _tech(engine, "samplain")
    _prop(engine, "PlainChipStop", today.weekday(), sam)
    html = client.get(f"/week?start={start}").text
    assert '<span class="pill p-acc" title="Samplain">S</span>' in html
    assert 'title="covering"' not in html


def test_disabled_cover_tech_renders_unassigned_not_question_chip(client, engine):
    # today.html/week.html chips look up tech_by_id (active techs only): a
    # cover tech disabled after the absence was created must render the
    # unassigned chip, not a bare "?" initials chip still titled "covering".
    wd = date.today().weekday()
    sam = _tech(engine, "samdisq")
    lee = _tech(engine, "leedisq")
    _prop(engine, "DisabledCoverStop", wd, sam)
    _absence(engine, sam, date.today(), date.today(), cover_id=lee)
    with Session(engine) as s:
        s.get(models.User, lee).active = False
        s.commit()
    html = client.get("/").text
    assert "DisabledCoverStop" in html
    assert 'title="covering"' not in html
    assert 'pill p-neg">unassigned' in html


def test_today_filter_bar_shows_counts(client, engine):
    wd = date.today().weekday()
    sam = _tech(engine, "sam13")
    lee = _tech(engine, "lee13")
    _prop(engine, "CountA", wd, sam)
    _prop(engine, "CountB", wd, sam)
    _prop(engine, "CountC", wd, lee)
    html = client.get("/").text
    assert "Sam13 (2)" in html
    assert "Lee13 (1)" in html


def test_week_filter_bar_shows_counts(client, engine):
    today = date.today()
    start = today - timedelta(days=today.weekday())
    sam = _tech(engine, "sam14")
    _prop(engine, "WeekCountA", today.weekday(), sam)
    html = client.get(f"/week?start={start}").text
    assert "Sam14 (1)" in html


# ---- users admin: time-off CRUD --------------------------------------------

def test_create_view_and_delete_absence(client, engine):
    sam = _tech(engine, "sam15")
    lee = _tech(engine, "lee15")
    r = client.post(f"/users/{sam}/absences/new", data={
        "start_date": "2026-08-01", "end_date": "2026-08-05",
        "cover_tech_id": str(lee), "note": "Vacation"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        a = s.scalars(select(models.TechAbsence)).one()
        assert a.user_id == sam
        assert a.cover_tech_id == lee
        assert a.note == "Vacation"
        aid = a.id
    html = client.get(f"/users/{sam}/edit").text
    assert "2026-08-01" in html and "2026-08-05" in html and "Vacation" in html

    r2 = client.post(f"/users/{sam}/absences/{aid}/delete", follow_redirects=False)
    assert r2.status_code == 303
    with Session(engine) as s:
        assert s.scalars(select(models.TechAbsence)).all() == []


def test_absence_validation_end_before_start(client, engine):
    sam = _tech(engine, "sam16")
    r = client.post(f"/users/{sam}/absences/new", data={
        "start_date": "2026-08-10", "end_date": "2026-08-05",
        "cover_tech_id": "", "note": ""})
    assert r.status_code == 400
    assert "end date" in r.text.lower()
    with Session(engine) as s:
        assert s.scalars(select(models.TechAbsence)).all() == []


def test_absence_validation_tolerant_bad_dates(client, engine):
    sam = _tech(engine, "sam17")
    r = client.post(f"/users/{sam}/absences/new", data={
        "start_date": "not-a-date", "end_date": "2026-08-05",
        "cover_tech_id": "", "note": ""})
    assert r.status_code == 400
    with Session(engine) as s:
        assert s.scalars(select(models.TechAbsence)).all() == []


def test_absence_cover_self_or_invalid_ignored(client, engine):
    sam = _tech(engine, "sam18")
    r = client.post(f"/users/{sam}/absences/new", data={
        "start_date": "2026-08-01", "end_date": "2026-08-02",
        "cover_tech_id": str(sam), "note": ""}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        a = s.scalars(select(models.TechAbsence)).one()
        assert a.cover_tech_id is None

    r2 = client.post(f"/users/{sam}/absences/new", data={
        "start_date": "2026-09-01", "end_date": "2026-09-02",
        "cover_tech_id": "999999", "note": ""}, follow_redirects=False)
    assert r2.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.TechAbsence)).all()
        assert all(a.cover_tech_id is None for a in rows)


def test_absence_routes_admin_only(client, engine, tech_client):
    sam = _tech(engine, "sam19")
    t = tech_client(name="plain19")
    r = t.post(f"/users/{sam}/absences/new", data={
        "start_date": "2026-08-01", "end_date": "2026-08-02",
        "cover_tech_id": "", "note": ""})
    assert r.status_code == 403
    r2 = t.get(f"/users/{sam}/edit")
    assert r2.status_code == 403

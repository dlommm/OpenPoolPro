"""Water tester integrations: parsing each source, and -- above all -- that a
reading only ever reaches the pool it belongs to."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, tester_clients, tester_import, water_readings
from app.db import Base
from app.timefmt import business_today

# ---------------------------------------------------------------------------
# parameter names and numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,key", [
    ("PL Chlorine Free", "fc"), ("PL Chlorine Total", "tc"), ("PL pH", "ph"), ("PL T-Alka", "ta"),
    ("PL Cyanuric Acid", "cya"), ("Free Chlorine (ppm)", "fc"), ("Combined Chlorine", "cc"),
    ("Total Alkalinity", "ta"), ("Total Hardness", "ch"), ("Calcium Hardness", "ch"),
    ("Salt", "salt"), ("Salinity (ppm)", "salt"), ("Water Temp (F)", "temp_c"), ("ORP", "orp"),
    ("pH", "ph"), ("Chlorine", "fc"), ("CYA", "cya"), ("Stabilizer", "cya"),
    ("Phosphate", None), ("Customer", None), ("Date", None), ("Iron", None), ("Bromine", None),
])
def test_param_key(name, key):
    assert water_readings.param_key(name) == key


@pytest.mark.parametrize("raw,val", [("7.4", 7.4), ("7,4", 7.4), ("<0.1", 0.1), (">8", 8.0),
                                     (" 1.20 ppm", 1.2), ("", None), ("n/a", None), ("nan", None), (3, 3.0)])
def test_parse_number(raw, val):
    assert water_readings.parse_number(raw) == val


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _customer(s, name, street="", label="Home"):
    c = models.Customer(name=name)
    s.add(c)
    s.flush()
    p = models.Property(customer_id=c.id, label=label, street=street)
    s.add(p)
    s.flush()
    return p


def _user(s, name):
    u = models.User(name=name, username=name.lower(), role="tech", active=True)
    s.add(u)
    s.flush()
    return u


def _visit(s, prop, completed_at=None):
    v = models.Visit(property_id=prop.id, date=business_today(), status="done",
                     completed_at=completed_at or models.utcnow())
    s.add(v)
    s.flush()
    return v


NOW = datetime(2026, 9, 26, 15, 0)


def _epoch(dt):
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _labcom_account(acct_id, name, measurements, street="1 Palm Ave"):
    return {"id": acct_id, "forename": "", "surname": name, "street": street, "zipcode": "", "city": "Tampa",
            "Measurements": measurements}


def _m(mid, param, value, at, serial="PL-1"):
    return {"id": mid, "parameter": param, "scenario": "", "unit": "ppm", "value": value,
            "timestamp": _epoch(at), "device_serial": serial, "operator_name": ""}


# ---------------------------------------------------------------------------
# LabCOM
# ---------------------------------------------------------------------------

def test_labcom_groups_one_sitting_into_one_test(db):
    acct = _labcom_account(7, "Smith", [
        _m(1, "PL Chlorine Free", "2.5", NOW - timedelta(minutes=10)),
        _m(2, "PL pH", "7.6", NOW - timedelta(minutes=8)),
        _m(3, "PL T-Alka", "90", NOW - timedelta(minutes=5)),
        _m(4, "PL pH", "7.2", NOW - timedelta(hours=3)),                       # earlier sitting
        _m(5, "PL pH", "7.0", NOW - timedelta(days=9)),                        # too old
        _m(6, "PL pH", "7.9", NOW - timedelta(minutes=9), serial="PL-2"),     # other tester
    ])
    assert tester_clients.labcom_ingest(db, [acct], NOW) == 3
    rows = db.scalars(select(models.DeviceReading).order_by(models.DeviceReading.measured_at)).all()
    assert [r.values for r in rows] == [{"ph": 7.2}, {"fc": 2.5, "ph": 7.6, "ta": 90.0}, {"ph": 7.9}]
    assert rows[1].external_key == "7" and "Smith" in rows[1].external_label
    assert {d.serial for d in db.scalars(select(models.TesterDevice))} == {"PL-1", "PL-2"}


def test_labcom_resync_adds_late_parameters_without_overwriting(db):
    first = [_m(1, "PL Chlorine Free", "2.5", NOW - timedelta(minutes=10))]
    tester_clients.labcom_ingest(db, [_labcom_account(7, "Smith", first)], NOW)
    later = first + [_m(2, "PL pH", "7.6", NOW - timedelta(minutes=6)),
                     _m(9, "PL Chlorine Free", "9.9", NOW - timedelta(minutes=4))]
    assert tester_clients.labcom_ingest(db, [_labcom_account(7, "Smith", later)], NOW) == 0
    (r,) = db.scalars(select(models.DeviceReading)).all()
    assert r.values == {"fc": 2.5, "ph": 7.6}


def test_labcom_link_ties_new_tests_on_arrival(db):
    prop = _customer(db, "Smith")
    water_readings.create_link(db, source="labcom", external_key="7", external_label="Smith",
                               property_id=prop.id, user_id=None)
    tester_clients.labcom_ingest(db, [_labcom_account(7, "Smith", [
        _m(1, "PL pH", "7.5", NOW - timedelta(minutes=1))])], NOW)
    (r,) = db.scalars(select(models.DeviceReading)).all()
    assert (r.property_id, r.match_method) == (prop.id, "link")


def test_labcom_fetch_errors_are_admin_safe(monkeypatch):
    monkeypatch.setattr(tester_clients, "_http", lambda *a, **k: (401, None))
    with pytest.raises(tester_clients.TesterError, match="rejected the API token"):
        tester_clients.labcom_fetch("secret-token-value")
    monkeypatch.setattr(tester_clients, "_http", lambda *a, **k: (200, {"errors": [{"message": "bad query"}]}))
    with pytest.raises(tester_clients.TesterError, match="bad query") as e:
        tester_clients.labcom_fetch("secret-token-value")
    assert "secret-token-value" not in str(e.value)


def test_labcom_fetch_sends_token_and_parses(monkeypatch):
    seen = {}

    def fake(method, url, headers=None, body=None, timeout=20):
        seen.update(method=method, url=url, headers=headers, body=json.loads(body))
        return 200, {"data": {"CloudAccount": {"Accounts": [{"id": 1}]}}}

    monkeypatch.setattr(tester_clients, "_http", fake)
    assert tester_clients.labcom_fetch("tok") == [{"id": 1}]
    assert seen["url"] == tester_clients.LABCOM_URL and seen["headers"]["Authorization"] == "tok"
    assert "Measurements" in seen["body"]["query"]


# ---------------------------------------------------------------------------
# Ondilo and iopool
# ---------------------------------------------------------------------------

def test_ondilo_measures_become_one_monitor_reading(db):
    prop = _customer(db, "Jones")
    link = water_readings.create_link(db, source="ondilo", external_key="234", external_label="Jones pool",
                                      property_id=prop.id, user_id=None)
    measures = [
        {"data_type": "temperature", "value": 26.5, "value_time": "2026-09-26T14:08:51+0000", "is_valid": True},
        {"data_type": "ph", "value": 7.4, "value_time": "2026-09-26T14:08:51+0000", "is_valid": True},
        {"data_type": "orp", "value": 700, "value_time": "2026-09-26T14:08:51+0000", "is_valid": True},
        {"data_type": "salt", "value": 3200, "value_time": "2026-09-26T14:08:51+0000", "is_valid": False},
        {"data_type": "battery", "value": 80, "value_time": "2026-09-26T14:08:51+0000", "is_valid": True},
    ]
    assert tester_clients.ondilo_ingest(db, link, measures) == 1
    assert tester_clients.ondilo_ingest(db, link, measures) == 0   # same hour: no duplicate
    (r,) = db.scalars(select(models.DeviceReading)).all()
    assert r.values == {"temp_c": 26.5, "ph": 7.4, "orp": 700.0}
    assert r.kind == "monitor" and r.property_id == prop.id
    assert r.measured_at == datetime(2026, 9, 26, 14, 8, 51)


def test_ondilo_refreshes_access_token_and_persists_rotation(db, monkeypatch):
    models.set_setting(db, "ondilo_refresh_token", "r1")
    tester_clients._ondilo_cache.clear()
    calls = []

    def fake(method, url, headers=None, body=None, timeout=20):
        calls.append((method, url, body))
        if url == tester_clients.ONDILO_TOKEN:
            return 200, {"access_token": "a1", "expires_in": 3600, "refresh_token": "r2"}
        assert headers["Authorization"] == "Bearer a1"
        return 200, [{"id": 5, "name": "Back yard", "address": {"street": "9 Oak", "city": "Tampa"}}]

    monkeypatch.setattr(tester_clients, "_http", fake)
    assert tester_clients.ondilo_pools(db) == [{"key": "5", "label": "Back yard — 9 Oak, Tampa"}]
    assert b"grant_type=refresh_token" in calls[0][2]
    assert models.get_setting(db, "ondilo_refresh_token", "") == "r2"
    tester_clients._ondilo_cache.clear()


def test_iopool_one_call_per_owner_key(db, monkeypatch):
    a, b = _customer(db, "A"), _customer(db, "B")
    la = water_readings.create_link(db, source="iopool", external_key="p1", external_label="A",
                                    property_id=a.id, user_id=None, secret="KEY1")
    lb = water_readings.create_link(db, source="iopool", external_key="p2", external_label="B",
                                    property_id=b.id, user_id=None, secret="KEY1")
    assert la.secret != "KEY1" and water_readings.link_secret(la) == "KEY1"   # encrypted at rest
    calls = []

    def fake(method, url, headers=None, body=None, timeout=20):
        calls.append(headers["x-api-key"])
        return 200, [
            {"id": "p1", "title": "A", "latestMeasure": {"temperature": 25, "ph": 7.3, "orp": 650, "isValid": True,
                                                          "ecoId": "E1", "measuredAt": "2026-09-26T14:00:00Z"}},
            {"id": "p2", "title": "B", "latestMeasure": {"temperature": 24, "ph": 7.8, "orp": 600, "isValid": False,
                                                          "ecoId": "E2", "measuredAt": "2026-09-26T14:00:00Z"}}]

    monkeypatch.setattr(tester_clients, "_http", fake)
    assert tester_clients.iopool_sync(db, NOW) == 1
    assert calls == ["KEY1"]
    (r,) = db.scalars(select(models.DeviceReading)).all()
    assert r.property_id == a.id and r.values == {"temp_c": 25.0, "ph": 7.3, "orp": 650.0}
    assert la.last_polled_at == NOW and lb.last_polled_at == NOW


def test_sync_all_isolates_a_failing_source(db, monkeypatch):
    models.set_setting(db, "labcom_token", "tok")

    def boom(token):
        raise tester_clients.TesterError("LabCOM returned HTTP 500.")

    monkeypatch.setattr(tester_clients, "labcom_fetch", boom)
    out = tester_clients.sync_all(db, NOW)
    assert out == {"labcom": "LabCOM returned HTTP 500."}
    assert tester_clients.status(db)["labcom"]["error"] == "LabCOM returned HTTP 500."


def test_credentials_are_encrypted_at_rest(db):
    models.set_setting(db, "labcom_token", "plain-token")
    models.set_setting(db, "ondilo_refresh_token", "plain-refresh")
    db.flush()
    raw = {k: db.get(models.Setting, k).value for k in ("labcom_token", "ondilo_refresh_token")}
    assert "plain" not in "".join(raw.values())


# ---------------------------------------------------------------------------
# file import
# ---------------------------------------------------------------------------

def _tests_from(csv_text, roles=None):
    rows = tester_import.read_rows(csv_text.encode())
    headers = rows[0]
    return tester_import.build_tests(headers, rows[1:], roles or tester_import.guess_roles(headers))


def test_import_wide_layout_with_title_rows_and_fahrenheit():
    csv_text = ("eXact iDip export\n\n"
                "Date,Time,Customer,Free Chlorine,pH,Total Alkalinity,Water Temp (F)\n"
                "09/26/2026,10:05 AM,Smith,2.5,7.6,90,77\n"
                "09/26/2026,11:15 AM,Jones,1.0,7.9,,\n"
                "not a date,,Ghost,1,1,1,1\n")
    tests, skipped = _tests_from(csv_text)
    assert skipped == 1
    assert [t["label"] for t in tests] == ["Smith", "Jones"]
    assert tests[0]["values"] == {"fc": 2.5, "ph": 7.6, "ta": 90.0, "temp_c": 25.0}
    assert tests[1]["values"] == {"fc": 1.0, "ph": 7.9}


def test_import_long_layout_groups_parameters():
    csv_text = ("Date,Time,Site,Test,Result,Units\n"
                "2026-09-26,10:05,Smith,Chlorine Free,2.5,ppm\n"
                "2026-09-26,10:05,Smith,pH,7.6,\n"
                "2026-09-26,10:05,Smith,Temperature,77,°F\n"
                "2026-09-26,10:30,Jones,pH,7.2,\n")
    tests, skipped = _tests_from(csv_text)
    assert skipped == 0
    by = {t["label"]: t["values"] for t in tests}
    assert by == {"Smith": {"fc": 2.5, "ph": 7.6, "temp_c": 25.0}, "Jones": {"ph": 7.2}}


def test_import_needs_a_date_column():
    rows = tester_import.read_rows(b"Customer,pH\nSmith,7.4\n")
    with pytest.raises(tester_import.TesterImportError, match="date"):
        tester_import.build_tests(rows[0], rows[1:], tester_import.guess_roles(rows[0]))


def test_import_is_idempotent(db):
    tests, _ = _tests_from("Date,Time,Customer,pH\n2026-09-26,10:05,Smith,7.4\n")
    for _ in range(2):
        for t in tests:
            water_readings.upsert(db, source="file", external_id=tester_import.external_id(t),
                                  measured_at=t["measured_at"], values=t["values"],
                                  external_key=water_readings.label_key(t["label"]), external_label=t["label"])
    assert len(db.scalars(select(models.DeviceReading)).all()) == 1


def test_parse_when_is_business_local():
    import app.config as config
    local = datetime(2026, 9, 26, 10, 5, tzinfo=config.tz())
    expected = local.astimezone(timezone.utc).replace(tzinfo=None)
    assert tester_import.parse_when("09/26/2026", "10:05 AM") == expected
    assert tester_import.parse_when("2026-09-26 10:05") == expected
    assert tester_import.parse_when("2026-09-26T14:05:00Z") == datetime(2026, 9, 26, 14, 5)
    assert tester_import.parse_when("garbage") is None


# ---------------------------------------------------------------------------
# tying readings to the right pool
# ---------------------------------------------------------------------------

def _reading(db, **kw):
    base = dict(source="labcom", external_id=f"x{datetime.now().timestamp()}{kw.get('device_serial', '')}",
                measured_at=models.utcnow() - timedelta(minutes=5), values={"fc": 2.0, "ph": 7.5})
    base.update(kw)
    r, _ = water_readings.upsert(db, **base)
    return r


def test_candidates_only_this_pool_or_my_own_tester(db):
    smith, jones = _customer(db, "Smith"), _customer(db, "Jones")
    sam, pat = _user(db, "Sam"), _user(db, "Pat")
    visit = models.Visit(property_id=smith.id, date=business_today(), status="due")
    db.add(visit)
    db.flush()
    mine = _reading(db, external_id="a", device_serial="SAM-1")
    theirs = _reading(db, external_id="b", device_serial="PAT-1")
    other_pool = _reading(db, external_id="c", device_serial="SAM-1")
    other_pool.property_id = jones.id
    this_pool = _reading(db, external_id="d")
    this_pool.property_id = smith.id
    stale = _reading(db, external_id="e", device_serial="SAM-1",
                     measured_at=models.utcnow() - timedelta(hours=6))
    uploaded = _reading(db, external_id="f", source="file", uploaded_by_id=sam.id)
    ignored = _reading(db, external_id="g", device_serial="SAM-1")
    ignored.status = "ignored"
    for d in db.scalars(select(models.TesterDevice)):
        d.user_id = sam.id if d.serial.startswith("SAM") else pat.id
    db.flush()
    got = {r.id for r in water_readings.candidates(db, visit, sam.id)}
    assert got == {mine.id, this_pool.id, uploaded.id}
    assert theirs.id not in got and other_pool.id not in got and stale.id not in got
    assert {r.id for r in water_readings.candidates(db, visit, pat.id)} == {theirs.id, this_pool.id}


def test_attach_ties_reading_and_flags_edits(db):
    smith = _customer(db, "Smith")
    sam = _user(db, "Sam")
    r = _reading(db, external_id="a", uploaded_by_id=sam.id, values={"fc": 2.0, "ph": 7.5})
    v = _visit(db, smith)
    v.fc, v.ph = 2.0, 7.5
    assert water_readings.attach_to_visit(db, v, str(r.id), sam) == ""
    assert (r.property_id, r.visit_id, r.match_method, r.edited) == (smith.id, v.id, "visit", False)
    v.ph = 7.2                                         # tech corrects pH on a re-save
    water_readings.attach_to_visit(db, v, None, sam)
    assert r.edited is True and r.visit_id == v.id


@pytest.mark.parametrize("case", ["other_pool", "used", "not_mine", "too_old", "ignored"])
def test_attach_refusals(db, case):
    smith, jones = _customer(db, "Smith"), _customer(db, "Jones")
    sam, pat = _user(db, "Sam"), _user(db, "Pat")
    r = _reading(db, external_id="a", uploaded_by_id=sam.id)
    v = _visit(db, smith)
    if case == "other_pool":
        r.property_id = jones.id
    elif case == "used":
        r.visit_id = _visit(db, smith).id
    elif case == "not_mine":
        r.uploaded_by_id = pat.id
    elif case == "too_old":
        r.measured_at = models.utcnow() - timedelta(days=2)
    elif case == "ignored":
        r.status = "ignored"
    db.flush()
    before = r.visit_id
    assert water_readings.attach_to_visit(db, v, str(r.id), sam) != ""
    assert r.visit_id == before
    assert db.scalars(select(models.AuditLog).where(models.AuditLog.action == "tester.use_refused")).first()


def test_name_warning(db):
    smith = _customer(db, "Jane Smith", street="12 Coral Way")
    r = _reading(db, external_id="a", external_label="Smith — 12 Coral Way")
    assert water_readings.name_warning(r, smith) == ""
    r.external_label = "Johnson"
    assert "Johnson" in water_readings.name_warning(r, smith)
    r.external_label = ""
    assert water_readings.name_warning(r, smith) == ""


def test_link_applies_to_waiting_but_never_moves_assigned_readings(db):
    smith, jones = _customer(db, "Smith"), _customer(db, "Jones")
    waiting = _reading(db, external_id="a", external_key="7")
    elsewhere = _reading(db, external_id="b", external_key="7")
    elsewhere.property_id = jones.id
    water_readings.create_link(db, source="labcom", external_key="7", external_label="Smith",
                               property_id=smith.id, user_id=None)
    assert waiting.property_id == smith.id and elsewhere.property_id == jones.id


def test_suggestions_rank_by_shared_words(db):
    _customer(db, "Jane Smith", street="12 Coral Way")
    _customer(db, "Bob Jones", street="4 Coral Way")
    r = _reading(db, external_id="a", external_label="Smith Coral")
    assert [p.customer.name for p in water_readings.suggestions(db, r)] == ["Jane Smith", "Bob Jones"]


def test_prune_keeps_readings_used_on_visits(db):
    smith = _customer(db, "Smith")
    old = models.utcnow() - timedelta(days=200)
    used = _reading(db, external_id="a", measured_at=old)
    used.visit_id = _visit(db, smith).id
    _reading(db, external_id="b", measured_at=old)
    assert water_readings.prune(db) == 1
    assert db.scalars(select(models.DeviceReading)).all() == [used]


# ---------------------------------------------------------------------------
# through the app
# ---------------------------------------------------------------------------

def _seed(client):
    client.post("/customers/new", data={"name": "Smith", "qb_display_name": "", "phone": "", "email": "", "notes": ""})
    client.post("/customers/1/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    today = business_today().isoformat()
    assert client.get(f"/visit/1/{today}").status_code == 200


def _complete(client, engine, **fields):
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        vid, status, revision = v.id, v.status, v.revision
    data = {"form_status": status, "revision": str(revision), "fc": "", "ph": "", "ta": "", "cya": "", "salt": "",
            "notes": "", "started_hhmm": "", "ended_hhmm": ""}
    data.update(fields)
    return client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False), vid


def test_visit_panel_use_and_complete_end_to_end(client, engine):
    _seed(client)
    with Session(engine) as s:
        owner = s.scalars(select(models.User).where(models.User.username == "owner")).one()
        r, _ = water_readings.upsert(s, source="file", external_id="t1", measured_at=models.utcnow(),
                                     values={"fc": 3.0, "ph": 7.4, "ch": 250.0}, uploaded_by_id=owner.id,
                                     external_label="Smith")
        s.commit()
        rid = r.id
        vid = s.scalars(select(models.Visit)).one().id
    panel = client.get(f"/tester-readings/visit/{vid}")
    assert panel.status_code == 200 and "Use these readings" in panel.text and "Smith" in panel.text
    grid = client.get(f"/tester-readings/visit/{vid}/use/{rid}", params={"ta": "80"})
    assert 'name="device_reading_id" value="%d"' % rid in grid.text
    assert 'value="3"' in grid.text and 'value="7.4"' in grid.text and 'value="80"' in grid.text
    assert "Calcium hardness 250 ppm" in grid.text
    assert 'id="tester-panel"' in grid.text and 'hx-swap-oob="true"' in grid.text   # list closes
    resp, _ = _complete(client, engine, fc="3", ph="7.4", ta="80", device_reading_id=str(rid))
    assert resp.status_code == 303
    with Session(engine) as s:
        r = s.get(models.DeviceReading, rid)
        assert (r.visit_id, r.property_id, r.edited) == (vid, 1, False)
    today = business_today().isoformat()
    assert "Saved readings came from Imported file" in client.get(f"/visit/1/{today}").text


def test_complete_with_foreign_reading_still_saves_visit(client, engine, tech_client):
    _seed(client)
    with Session(engine) as s:
        other = models.User(name="Pat", username="pat", role="tech", active=True)
        s.add(other)
        s.flush()
        r, _ = water_readings.upsert(s, source="file", external_id="t1", measured_at=models.utcnow(),
                                     values={"fc": 3.0}, uploaded_by_id=other.id)
        s.commit()
        rid = r.id
    resp, vid = _complete(client, engine, fc="3", device_reading_id=str(rid))
    assert resp.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Visit, vid).fc == 3.0
        assert s.get(models.DeviceReading, rid).visit_id is None
    assert client.get(f"/tester-readings/visit/{vid}/use/{rid}").status_code == 409


def test_import_flow_and_inbox_assign_with_remember(client, engine):
    _seed(client)
    csv_bytes = b"Date,Time,Customer,Free Chlorine,pH\n09/26/2026,10:05 AM,Smith,2.5,7.6\n"
    page = client.post("/water-tests/import", files={"file": ("idip.csv", csv_bytes, "text/csv")})
    assert page.status_code == 200 and "Check the columns" in page.text
    import re
    token = re.search(r"/water-tests/import/([0-9a-f]{32})", page.text).group(1)
    roles = {f"role_{i}": r for i, r in enumerate(["date", "time", "label", "p:fc", "p:ph"])}
    done = client.post(f"/water-tests/import/{token}", data=roles)
    assert "1</strong> new test(s) imported" in done.text
    inbox = client.get("/water-tests")
    assert "Smith" in inbox.text and "Suggested" in inbox.text
    with Session(engine) as s:
        rid = s.scalars(select(models.DeviceReading)).one().id
    client.post(f"/water-tests/{rid}/assign", data={"property_id": "1", "remember": "1"})
    with Session(engine) as s:
        r = s.get(models.DeviceReading, rid)
        assert (r.property_id, r.match_method) == (1, "manual")
        link = s.scalars(select(models.TesterLink)).one()
        assert (link.source, link.external_key, link.property_id) == ("file", "smith", 1)
    # The next export naming Smith goes straight to that pool.
    page = client.post("/water-tests/import", files={
        "file": ("idip.csv", b"Date,Time,Customer,pH\n09/27/2026,9:00 AM,SMITH,7.3\n", "text/csv")})
    token = re.search(r"/water-tests/import/([0-9a-f]{32})", page.text).group(1)
    client.post(f"/water-tests/import/{token}",
                data={f"role_{i}": r for i, r in enumerate(["date", "time", "label", "p:ph"])})
    with Session(engine) as s:
        newest = s.scalars(select(models.DeviceReading).order_by(models.DeviceReading.id.desc())).first()
        assert (newest.property_id, newest.match_method) == (1, "link")


def test_inbox_cannot_move_a_reading_already_on_a_visit(client, engine):
    _seed(client)
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        r, _ = water_readings.upsert(s, source="file", external_id="t1", measured_at=models.utcnow(), values={"ph": 7})
        r.property_id, r.visit_id = v.property_id, v.id
        s.commit()
        rid = r.id
    assert client.post(f"/water-tests/{rid}/unassign").status_code == 409
    assert client.post(f"/water-tests/{rid}/assign", data={"property_id": "1"}).status_code == 409


def test_import_token_belongs_to_uploader(client, tech_client):
    page = client.post("/water-tests/import", files={
        "file": ("t.csv", b"Date,pH\n2026-09-26,7.4\n", "text/csv")})
    import re
    token = re.search(r"/water-tests/import/([0-9a-f]{32})", page.text).group(1)
    sam = tech_client("sam")
    assert sam.post(f"/water-tests/import/{token}", data={"role_0": "date", "role_1": "p:ph"}).status_code == 403
    assert sam.post("/water-tests/import/..%2f..%2fetc", data={}).status_code in (200, 404)


def test_permissions(client, tech_client):
    sam = tech_client("sam")
    for path in ("/testers", "/water-tests"):
        assert sam.get(path, follow_redirects=False).status_code in (303, 403)
    assert sam.get("/water-tests/import").status_code == 200
    assert sam.post("/testers/labcom", data={"token": "x"}, follow_redirects=False).status_code in (303, 403)
    mgr = tech_client("mgr", manage_customers=True)
    assert mgr.get("/water-tests").status_code == 200
    assert client.get("/testers").status_code == 200


def test_ondilo_callback_rejects_forged_state(client, monkeypatch):
    called = []
    monkeypatch.setattr(tester_clients, "ondilo_complete", lambda *a, **k: called.append(1))
    r = client.get("/testers/ondilo/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 303 and called == []
    assert "could not be verified" in client.get("/testers").text


def test_ondilo_connect_then_callback(client, monkeypatch):
    r = client.post("/testers/ondilo/connect", follow_redirects=False)
    assert r.headers["location"].startswith(tester_clients.ONDILO_AUTHORIZE)
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    got = {}
    monkeypatch.setattr(tester_clients, "ondilo_complete",
                        lambda session, code, redirect: got.update(code=code, redirect=redirect))
    client.get("/testers/ondilo/callback", params={"code": "c1", "state": state})
    assert got["code"] == "c1" and got["redirect"].endswith("/testers/ondilo/callback")

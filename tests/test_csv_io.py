from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

EXPORT_HEADER = ("customer_name,qb_display_name,phone,email,customer_notes,"
                 "property_label,address,gate_code,dog_note,frequency,weekday,"
                 "rate_override_dollars,assigned_tech,pool_shape,length_ft,width_ft,"
                 "avg_depth_ft,water_type")


def _seed_full(client, engine):
    client.post("/customers/new", data={"name": "Csv Family", "qb_display_name": "Csv LLC",
                                        "phone": "555-1", "email": "c@x.com", "notes": "vip"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "1 Way", "gate_code": "77", "dog_note": "",
        "notes": "", "frequency": "biweekly", "weekday": "2", "anchor_date": "2026-07-08",
        "rate_override_dollars": "62.50", "assigned_tech_id": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})
    return cid


def test_export_shape(client, engine):
    _seed_full(client, engine)
    r = client.get("/customers/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == EXPORT_HEADER
    row = lines[1]
    assert "Csv Family" in row and "62.50" in row and "rectangle" in row and "biweekly" in row


def test_export_admin_only(client, tech_client):
    t = tech_client(name="nope", manage_customers=True)
    assert t.get("/customers/export", follow_redirects=False).status_code == 403


def test_export_writes_export_customers_audit_row(client, engine):
    _seed_full(client, engine)
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.get("/customers/export")
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "export.customers")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id




CSV_GOOD = """name,phone,addr,day,shape,len,wid,depth
Alpha Pools,555-9,9 Alpha St,1,rectangle,30,15,5
Beta Home,555-8,8 Beta Ave,2,,,,
"""


def _upload(client, text):
    return client.post("/customers/import", files={
        "file": ("import.csv", text.encode(), "text/csv")})


def _mapping_data(html):
    # extract token from the form action in mapping page
    import re
    m = re.search(r"/customers/import/([a-f0-9]{32})/preview", html)
    assert m, "token not found in mapping page"
    return m.group(1)


def test_import_full_flow(client, engine):
    r = _upload(client, CSV_GOOD)
    assert r.status_code == 200
    token = _mapping_data(r.text)
    mapping = {"map_customer_name": "name", "map_phone": "phone", "map_address": "addr",
               "map_weekday": "day", "map_pool_shape": "shape", "map_length_ft": "len",
               "map_width_ft": "wid", "map_avg_depth_ft": "depth"}
    r = client.post(f"/customers/import/{token}/preview", data=mapping)
    assert r.status_code == 200 and "ok" in r.text
    r = client.post(f"/customers/import/{token}/commit", data=mapping,
                    follow_redirects=False)
    assert r.status_code == 200
    with Session(engine) as s:
        c = s.scalars(select(models.Customer).where(
            models.Customer.name == "Alpha Pools")).one()
        p = c.properties[0]
        assert p.address == "9 Alpha St" and p.weekday == 1
        assert p.pools[0].gallons == 16875
        beta = s.scalars(select(models.Customer).where(
            models.Customer.name == "Beta Home")).one()
        assert beta.properties[0].pools == []


def test_import_commit_writes_import_commit_audit_row(client, engine):
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    r = _upload(client, CSV_GOOD)
    token = _mapping_data(r.text)
    mapping = {"map_customer_name": "name", "map_phone": "phone", "map_address": "addr",
               "map_weekday": "day", "map_pool_shape": "shape", "map_length_ft": "len",
               "map_width_ft": "wid", "map_avg_depth_ft": "depth"}
    client.post(f"/customers/import/{token}/preview", data=mapping)
    client.post(f"/customers/import/{token}/commit", data=mapping)
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "import.commit")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id
        assert "created=2" in rows[0].detail


def test_import_error_row_aborts_writes_no_audit_row(client, engine):
    bad = "name,day\nGood One,1\nBad Day,9\n"
    r = _upload(client, bad)
    token = _mapping_data(r.text)
    mapping = {"map_customer_name": "name", "map_weekday": "day"}
    client.post(f"/customers/import/{token}/commit", data=mapping)
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "import.commit")).all()
        assert len(rows) == 0


def test_import_duplicate_skipped(client, engine):
    client.post("/customers/new", data={"name": "Alpha Pools", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    r = _upload(client, CSV_GOOD)
    token = _mapping_data(r.text)
    mapping = {"map_customer_name": "name"}
    r = client.post(f"/customers/import/{token}/preview", data=mapping)
    assert "duplicate" in r.text
    client.post(f"/customers/import/{token}/commit", data=mapping)
    with Session(engine) as s:
        assert len(s.scalars(select(models.Customer).where(
            models.Customer.name == "Alpha Pools")).all()) == 1  # not duplicated


def test_import_error_row_aborts_batch(client, engine):
    bad = "name,day\nGood One,1\nBad Day,9\n"
    r = _upload(client, bad)
    token = _mapping_data(r.text)
    mapping = {"map_customer_name": "name", "map_weekday": "day"}
    r = client.post(f"/customers/import/{token}/commit", data=mapping)
    assert "error" in r.text.lower()
    with Session(engine) as s:
        assert s.scalars(select(models.Customer).where(
            models.Customer.name == "Good One")).first() is None  # all-or-nothing


def test_import_rejects_bad_files(client):
    r = client.post("/customers/import", files={
        "file": ("x.csv", b"", "text/csv")})
    assert "empty" in r.text.lower() or "no rows" in r.text.lower()
    r = client.get("/customers/import/deadbeef/preview")
    assert r.status_code in (404, 405)


def test_import_admin_only(client, tech_client):
    t = tech_client(name="noimp", manage_customers=True)
    assert t.get("/customers/import", follow_redirects=False).status_code == 403

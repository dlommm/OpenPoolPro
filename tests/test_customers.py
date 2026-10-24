from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def test_create_and_search_customer(client, engine):
    r = client.post("/customers/new", data={
        "name": "Smith Family", "qb_display_name": "", "phone": "555-1111",
        "email": "s@x.com", "notes": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "Smith Family" in client.get("/customers").text
    assert "Smith Family" in client.get("/customers?q=smi").text
    assert "Smith Family" not in client.get("/customers?q=zzz").text
    with Session(engine) as s:
        c = s.scalars(select(models.Customer)).one()
        assert c.qb_display_name == "Smith Family"  # defaults to name when blank


def test_add_property_with_schedule_and_override(client, engine):
    client.post("/customers/new", data={"name": "Jones", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Beach house", "street": "1 Ocean Dr", "gate_code": "1234",
        "dog_warning": "on", "dog_note": "Big dog", "notes": "",
        "frequency": "biweekly", "weekday": "2", "anchor_date": "2026-07-08",
        "rate_override_dollars": "62.50"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.scalars(select(models.Property)).one()
        assert p.frequency == "biweekly" and p.weekday == 2
        assert p.anchor_date == date(2026, 7, 8)
        assert p.rate_override_cents == 6250
    detail = client.get(f"/customers/{cid}").text
    assert "Beach house" in detail and "1234" in detail


def test_deactivate_customer_hides_from_list(client, engine):
    client.post("/customers/new", data={"name": "Old Guy", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/deactivate")
    assert "Old Guy" not in client.get("/customers").text


def test_deactivate_buttons_present_in_edit_forms(client, engine):
    # I1 pin: the deactivate action must be reachable from the edit forms.
    client.post("/customers/new", data={"name": "Btn Guy", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    customer_html = client.get(f"/customers/{cid}/edit").text
    assert f"/customers/{cid}/deactivate" in customer_html

    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "1 Bay St", "gate_code": "", "dog_note": "",
        "notes": "", "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    property_html = client.get(f"/properties/{pid}/edit").text
    assert f"/properties/{pid}/deactivate" in property_html


def test_deactivated_customer_stop_removed_from_schedule(client, engine):
    # I1 pin: deactivating a customer must pull their still-scheduled weekly
    # stop out of stops_for_date (and therefore out of Missed) even though
    # the underlying property row is untouched.
    from datetime import date, timedelta

    from app.scheduling import missed_stops, stops_for_date

    today = date(2026, 7, 15)  # Wednesday
    with Session(engine) as s:
        c = models.Customer(name="Dropped Guy")
        p = models.Property(customer=c, frequency="weekly", weekday=0,
                            anchor_date=today - timedelta(days=28))
        s.add(p)
        s.commit()
        cid, pid = c.id, p.id
        monday = today - timedelta(days=2)
        assert any(s2.property.id == pid for s2 in stops_for_date(s, monday))
        assert any(m.property.id == pid for m in missed_stops(s, today))

    client.post(f"/customers/{cid}/deactivate")

    with Session(engine) as s:
        assert not any(s2.property.id == pid for s2 in stops_for_date(s, monday))
        assert not any(m.property.id == pid for m in missed_stops(s, today))


def test_missing_records_return_404(client):
    assert client.get("/customers/999").status_code == 404
    assert client.get("/customers/999/edit").status_code == 404
    assert client.get("/properties/999/edit").status_code == 404
    assert client.post("/customers/999/deactivate").status_code == 404
    assert client.post("/properties/999/deactivate").status_code == 404
    assert client.post("/customers/999/edit", data={"name": "X", "qb_display_name": "",
                                                    "phone": "", "email": "", "notes": ""}).status_code == 404
    assert client.get("/customers/999/properties/new").status_code == 404
    assert client.post("/properties/999/edit", data={
        "label": "H", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": ""}).status_code == 404


def test_history_shows_duration(client, engine):
    from datetime import datetime as dt
    client.post("/customers/new", data={"name": "DurCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(
            models.Customer.name == "DurCo")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": ""})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).join(models.Customer).where(
            models.Customer.name == "DurCo")).one()
        s.add(models.Visit(property_id=p.id, date=dt(2026, 7, 13).date(), status="done",
                           started_at=dt(2026, 7, 13, 13, 0),
                           completed_at=dt(2026, 7, 13, 13, 42)))
        s.commit()
    assert "42 min" in client.get(f"/customers/{cid}").text


def test_timeline_renders_date_only_and_localized_instant_times(client, engine):
    from datetime import datetime as dt
    client.post("/customers/new", data={"name": "TZCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == "TZCo")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "",
        "rate_override_dollars": "", "assigned_tech_id": ""})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).join(models.Customer).where(
            models.Customer.name == "TZCo")).one()
        # Skipped without ever being opened -- no completed_at recorded, so
        # the timeline falls back to the visit's calendar date only.
        s.add(models.Visit(property_id=p.id, date=date(2026, 1, 15), status="skipped"))
        # A real instant: 13:30 UTC on 2026-07-20 is 09:30 America/New_York (EDT).
        s.add(models.CommLog(customer_id=cid, kind="call", note="Instant entry",
                             at=dt(2026, 7, 20, 13, 30)))
        s.commit()

    import re
    # Links may carry ISO dates (/visit/<pid>/2026-01-15); what a person reads
    # may not, so check the page with link targets removed.
    detail = re.sub(r'href="[^"]*"', "", client.get(f"/customers/{cid}").text)
    # Rendered for a person ("Jan 15, 2026"), never as bare ISO. Each guarantee
    # below is the same one this test has always made, in the new format.
    assert "Jan 15, 2026" in detail
    assert "2026-01-15" not in detail           # no bare ISO anywhere
    assert "Jan 15, 2026 12:00 AM" not in detail  # date-only: no fabricated midnight
    assert "Jul 20, 2026 9:30 AM" in detail     # localized, not raw UTC
    assert "Jul 20, 2026 1:30 PM" not in detail  # raw naive-UTC must not leak through


def test_reactivate_customer(client, engine):
    client.post("/customers/new", data={"name": "Snowbird", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/deactivate")
    assert "Snowbird" not in client.get("/customers").text
    assert "Snowbird" in client.get("/customers?show=inactive").text
    r = client.post(f"/customers/{cid}/reactivate", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Customer, cid).active is True
    assert "Snowbird" in client.get("/customers").text


def test_reactivate_property(client, engine):
    client.post("/customers/new", data={"name": "PropCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Cabana", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/deactivate")
    detail = client.get(f"/customers/{cid}").text
    assert "Inactive" in detail and "Reactivate property" in detail
    r = client.post(f"/properties/{pid}/reactivate", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Property, pid).active is True


def test_tag_add_remove_and_list_filter(client, engine):
    client.post("/customers/new", data={"name": "Tagged Co", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/tags/add", data={"tags": "VIP, Pool Club"})
    with Session(engine) as s:
        c = s.get(models.Customer, cid)
        assert c.tags == ["vip", "pool club"]
    assert "Tagged Co" in client.get("/customers?tag=vip").text
    assert "Tagged Co" not in client.get("/customers?tag=nope").text
    client.post(f"/customers/{cid}/tags/remove", data={"tag": "vip"})
    with Session(engine) as s:
        assert s.get(models.Customer, cid).tags == ["pool club"]


def test_tag_with_ampersand_is_urlencoded_in_list_links(client, engine):
    client.post("/customers/new", data={"name": "Rowdy Co", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/tags/add", data={"tags": "R&R x"})
    with Session(engine) as s:
        assert s.get(models.Customer, cid).tags == ["r&r x"]
    body = client.get("/customers").text
    assert "tag=r%26r" in body  # ampersand urlencoded; raw '&' would corrupt the query string
    assert "tag=r&r x" not in body
    assert "Rowdy Co" in client.get("/customers?tag=r%26r+x").text


def test_comm_log_quick_add_stamps_user_and_shows_in_timeline(client, engine):
    client.post("/customers/new", data={"name": "Comm Co", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/comm/add", data={"kind": "call", "note": "Discussed algae"})
    with Session(engine) as s:
        log = s.scalars(select(models.CommLog)).one()
        assert log.kind == "call" and log.note == "Discussed algae"
        assert log.user_id is not None
    detail = client.get(f"/customers/{cid}").text
    assert "Discussed algae" in detail and "Call" in detail


def test_hold_create_validation_and_end_now(client, engine):
    client.post("/customers/new", data={"name": "HoldCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id

    # invalid: resume <= start -> rejected, form re-rendered with error, nothing created
    r = client.post(f"/properties/{pid}/holds/new", data={
        "start_date": "2026-07-20", "resume_date": "2026-07-10", "reason": "Vacation"})
    assert r.status_code == 400
    assert "Resume date must be after" in r.text
    with Session(engine) as s:
        assert s.scalars(select(models.ServiceHold)).all() == []

    # valid
    r = client.post(f"/properties/{pid}/holds/new", data={
        "start_date": "2026-07-20", "resume_date": "2026-08-01", "reason": "Vacation"},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        hold = s.scalars(select(models.ServiceHold)).one()
        assert hold.start_date == date(2026, 7, 20)
        assert hold.resume_date == date(2026, 8, 1)
        hid = hold.id
    assert "2026-08-01" in client.get(f"/properties/{pid}/edit").text

    # end now
    from app.routers.today import business_today
    client.post(f"/properties/{pid}/holds/{hid}/end")
    with Session(engine) as s:
        assert s.get(models.ServiceHold, hid).resume_date == business_today()


def test_checklist_add_deactivate_reorder_and_visit_form_override(client, engine):
    client.post("/customers/new", data={"name": "ChecklistCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "2026-07-13",
        "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    with Session(engine) as s:
        s.add(models.TaskTemplate(label="Global Skim", sort_order=0))
        s.commit()

    # No override yet -> global template list is used
    visit_html = client.get(f"/visit/{pid}/2026-07-13").text
    assert "Global Skim" in visit_html

    # Add two property-specific tasks -> they replace the global list entirely
    client.post(f"/properties/{pid}/tasks/new", data={"label": "Brush walls"})
    client.post(f"/properties/{pid}/tasks/new", data={"label": "Empty skimmer"})
    visit_html2 = client.get(f"/visit/{pid}/2026-07-13").text
    assert "Brush walls" in visit_html2 and "Empty skimmer" in visit_html2
    assert "Global Skim" not in visit_html2

    with Session(engine) as s:
        tasks = s.scalars(select(models.PropertyTask).where(
            models.PropertyTask.property_id == pid).order_by(models.PropertyTask.sort_order)).all()
        assert [t.label for t in tasks] == ["Brush walls", "Empty skimmer"]
        first_id, second_id = tasks[0].id, tasks[1].id

    # Move the second one up -> order swaps
    client.post(f"/properties/{pid}/tasks/{second_id}/move", data={"direction": "up"})
    with Session(engine) as s:
        tasks = s.scalars(select(models.PropertyTask).where(
            models.PropertyTask.property_id == pid).order_by(models.PropertyTask.sort_order)).all()
        assert [t.label for t in tasks] == ["Empty skimmer", "Brush walls"]

    # Deactivate one -> only the remaining active one shows, and once none
    # are active the global template list applies again
    client.post(f"/properties/{pid}/tasks/{first_id}/deactivate")
    client.post(f"/properties/{pid}/tasks/{second_id}/deactivate")
    visit_html3 = client.get(f"/visit/{pid}/2026-07-13").text
    assert "Global Skim" in visit_html3


def test_rules_crud_create_edit_tolerant_parse_and_deactivate(client, engine):
    client.post("/customers/new", data={"name": "RulesCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id

    # create with garbage interval/lead -> tolerant fallback to sane defaults
    client.post(f"/properties/{pid}/rules/new", data={
        "title": "Filter clean", "description": "", "job_type": "service",
        "interval_weeks": "not-a-number", "lead_days": "-5", "next_due": "2026-08-01"})
    with Session(engine) as s:
        rule = s.scalars(select(models.MaintenanceRule)).one()
        assert rule.interval_weeks >= 1
        assert rule.lead_days >= 0
        assert rule.next_due == date(2026, 8, 1)
        rid = rule.id

    # edit: valid interval/lead update; invalid ones keep the previous value
    client.post(f"/properties/{pid}/rules/{rid}/edit", data={
        "title": "Filter clean v2", "description": "desc", "job_type": "repair",
        "interval_weeks": "2", "lead_days": "bad", "next_due": "2026-09-01"})
    with Session(engine) as s:
        rule = s.get(models.MaintenanceRule, rid)
        assert rule.title == "Filter clean v2"
        assert rule.job_type == "repair"
        assert rule.interval_weeks == 2
        assert rule.lead_days >= 0  # kept previous (bad input rejected)
        assert rule.next_due == date(2026, 9, 1)
        prior_lead_days = rule.lead_days

    # a non-positive interval on edit is rejected client-side too (kept previous)
    client.post(f"/properties/{pid}/rules/{rid}/edit", data={
        "title": "Filter clean v2", "description": "desc", "job_type": "repair",
        "interval_weeks": "0", "lead_days": str(prior_lead_days), "next_due": "2026-09-01"})
    with Session(engine) as s:
        assert s.get(models.MaintenanceRule, rid).interval_weeks == 2  # unchanged

    # deactivate
    client.post(f"/properties/{pid}/rules/{rid}/deactivate")
    with Session(engine) as s:
        assert s.get(models.MaintenanceRule, rid).active is False


def test_customer_photos_section_ptag_filter(client, engine):
    import io

    from PIL import Image

    def jpeg_bytes():
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), "blue").save(buf, format="JPEG")
        return buf.getvalue()

    client.post("/customers/new", data={"name": "PhotoCo", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "20", "width_ft": "10",
        "avg_depth_ft": "5", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id

    client.post(f"/photos/pool/{pool_id}", data={"tag": "before"},
               files=[("files", ("b.jpg", jpeg_bytes(), "image/jpeg"))])
    client.post(f"/photos/pool/{pool_id}", data={"tag": "issue"},
               files=[("files", ("i.jpg", jpeg_bytes(), "image/jpeg"))])
    with Session(engine) as s:
        before_id = s.scalars(select(models.Photo).where(models.Photo.tag == "before")).one().id
        issue_id = s.scalars(select(models.Photo).where(models.Photo.tag == "issue")).one().id

    all_photos = client.get(f"/customers/{cid}").text
    assert f"/photos/file/{before_id}" in all_photos
    assert f"/photos/file/{issue_id}" in all_photos

    only_issue = client.get(f"/customers/{cid}?ptag=issue").text
    assert f"/photos/file/{issue_id}" in only_issue
    assert f"/photos/file/{before_id}" not in only_issue

    # tolerant: unknown ptag value -> all photos shown, not an error
    tolerant = client.get(f"/customers/{cid}?ptag=nonsense").text
    assert tolerant.count("/photos/file/") >= 2


def test_notify_email_toggle_persists(client, engine):
    r = client.post("/customers/new", data={
        "name": "Notify Co", "qb_display_name": "", "phone": "", "email": "n@x.com",
        "notes": "", "notify_email": "on"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.scalars(select(models.Customer).where(models.Customer.name == "Notify Co")).one()
        assert c.notify_email is True
        cid = c.id

    edit_html = client.get(f"/customers/{cid}/edit").text
    assert "checked" in edit_html.split('name="notify_email"')[1].split(">")[0]

    # unchecking (omitting the field) turns it back off
    client.post(f"/customers/{cid}/edit", data={
        "name": "Notify Co", "qb_display_name": "", "phone": "", "email": "n@x.com",
        "notes": ""})
    with Session(engine) as s:
        assert s.get(models.Customer, cid).notify_email is False


def test_property_spa_override_saved(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Cara")
        s.add(c); s.commit(); cid = c.id
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "frequency": "weekly", "weekday": "0",
        "spa_rate_override_dollars": "25.00"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.scalars(select(models.Property).where(models.Property.customer_id == cid)).one()
        assert p.spa_rate_override_cents == 2500


def test_property_structured_address_composed(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import models
    client.post("/customers/new", data={"name": "Addr Co"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == "Addr Co")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "12 Palm Ave", "city": "Cocoa Beach",
        "state": "FL", "zip": "32931", "frequency": "weekly", "weekday": "0"})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).where(models.Property.customer_id == cid)).one()
        assert p.city == "Cocoa Beach" and p.state == "FL"
        assert p.address == "12 Palm Ave, Cocoa Beach, FL 32931"


def test_property_save_stores_geocode_when_key_set(client, engine, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import geocode, models
    monkeypatch.setattr(geocode, "geocode", lambda text, key, **kw: (28.3, -80.6))
    with Session(engine) as s:
        models.set_setting(s, "geoapify_key", "TESTKEY"); s.commit()
    client.post("/customers/new", data={"name": "Geo Co"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == "Geo Co")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "1 A St", "city": "Cocoa Beach", "state": "FL",
        "zip": "32931", "frequency": "weekly", "weekday": "0"})
    with Session(engine) as s:
        p = s.scalars(select(models.Property).where(models.Property.customer_id == cid)).one()
        assert p.latitude == 28.3 and p.longitude == -80.6


def test_property_save_survives_geocode_failure(client, engine, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import geocode, models
    def boom(text, key, **kw): raise RuntimeError("boom")
    monkeypatch.setattr(geocode, "geocode", boom)
    with Session(engine) as s:
        models.set_setting(s, "geoapify_key", "TESTKEY"); s.commit()
    client.post("/customers/new", data={"name": "Safe Co"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == "Safe Co")).one().id
    r = client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "1 A St", "city": "Cocoa Beach", "state": "FL",
        "zip": "32931", "frequency": "weekly", "weekday": "0"})
    assert r.status_code in (200, 303)  # save did not blow up


def test_edit_does_not_regeocode_when_address_unchanged(client, engine, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import geocode, models

    calls = {"n": 0}

    def counting_geocode(text, key, **kw):
        calls["n"] += 1
        return (28.3, -80.6)

    monkeypatch.setattr(geocode, "geocode", counting_geocode)
    with Session(engine) as s:
        models.set_setting(s, "geoapify_key", "TESTKEY"); s.commit()

    client.post("/customers/new", data={"name": "Regeo Co"})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == "Regeo Co")).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "street": "1 A St", "city": "Cocoa Beach", "state": "FL",
        "zip": "32931", "gate_code": "0000", "frequency": "weekly", "weekday": "0"})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property).where(models.Property.customer_id == cid)).one().id

    assert calls["n"] == 1
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        assert (p.latitude, p.longitude) == (28.3, -80.6)

    # Edit ONLY the gate code -- same street/city/state/zip -- must not re-geocode.
    client.post(f"/properties/{pid}/edit", data={
        "label": "Home", "street": "1 A St", "city": "Cocoa Beach", "state": "FL",
        "zip": "32931", "gate_code": "9999", "frequency": "weekly", "weekday": "0"})

    assert calls["n"] == 1  # unchanged: no second geocode call
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        assert p.gate_code == "9999"
        assert (p.latitude, p.longitude) == (28.3, -80.6)  # coords untouched

    # Now change the street -- must re-geocode.
    client.post(f"/properties/{pid}/edit", data={
        "label": "Home", "street": "2 B St", "city": "Cocoa Beach", "state": "FL",
        "zip": "32931", "gate_code": "9999", "frequency": "weekly", "weekday": "0"})
    assert calls["n"] == 2


def _mk_customer(client, engine, name, city="", street="1 A St"):
    client.post("/customers/new", data={"name": name})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer).where(models.Customer.name == name)).one().id
    if city:
        client.post(f"/customers/{cid}/properties/new", data={
            "label": "Home", "street": street, "city": city, "state": "FL",
            "zip": "32931", "frequency": "weekly", "weekday": "0"})
    return cid


def test_list_letter_filter(client, engine):
    _mk_customer(client, engine, "Alpha Co")
    _mk_customer(client, engine, "Bravo Co")
    body = client.get("/customers?letter=A").text
    assert "Alpha Co" in body and "Bravo Co" not in body


def test_list_city_filter(client, engine):
    _mk_customer(client, engine, "Coco Co", city="Cocoa Beach")
    _mk_customer(client, engine, "Melb Co", city="Melbourne")
    body = client.get("/customers?city=Cocoa+Beach").text
    assert "Coco Co" in body and "Melb Co" not in body


def test_list_pagination(client, engine):
    for i in range(60):
        _mk_customer(client, engine, f"Cust {i:03d}")
    page1 = client.get("/customers").text
    page2 = client.get("/customers?page=2").text

    # Default sort is by name; zero-padded names sort lexically = numerically,
    # so page 1 (offset 0, PAGE_SIZE=50) holds Cust 000..049 and page 2 holds
    # the remaining Cust 050..059.
    assert "Cust 000" in page1
    assert "Cust 049" in page1
    assert "Cust 050" not in page1
    assert "Cust 000" not in page2
    assert "Cust 050" in page2
    assert "Cust 059" in page2

    # Each page is bounded by PAGE_SIZE and the two pages don't overlap.
    assert page1.count("Cust ") == 50
    assert page2.count("Cust ") == 10


def test_list_search_matches_phone(client, engine):
    client.post("/customers/new", data={"name": "PhoneGuy", "phone": "555-7777"})
    body = client.get("/customers?q=7777").text
    assert "PhoneGuy" in body


def test_list_search_excludes_deactivated_property_address(client, engine):
    # Regression: the `q` search's EXISTS-over-properties subquery must only
    # match ACTIVE properties, same as the sibling `city` filter. A customer
    # kept active via a still-active property must not be surfaced by the
    # stale address/city of a DEACTIVATED property.
    cid = _mk_customer(client, engine, "Ghost Customer", city="Cocoa Beach")
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Old place", "street": "9999 Ghost Rd", "city": "Ghosttown",
        "state": "FL", "zip": "32931", "frequency": "weekly", "weekday": "0"})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property).where(
            models.Property.city == "Ghosttown")).one().id
    client.post(f"/properties/{pid}/deactivate")

    # Searching by the inactive property's city or address must not match.
    assert "Ghost Customer" not in client.get("/customers?q=Ghosttown").text
    assert "Ghost Customer" not in client.get("/customers?q=9999+Ghost+Rd").text

    # The customer is still active (via the other, active property) and
    # remains findable by name.
    assert "Ghost Customer" in client.get("/customers?q=Ghost+Customer").text


def test_list_sort_recent(client, engine):
    _mk_customer(client, engine, "Older Co")
    _mk_customer(client, engine, "Newer Co")
    body = client.get("/customers?sort=recent").text
    assert body.index("Newer Co") < body.index("Older Co")


def test_list_renders_table_and_controls(client, engine):
    _mk_customer(client, engine, "Table Co", city="Cocoa Beach")
    body = client.get("/customers").text
    assert "<table" in body
    assert 'href="/customers?letter=A' in body            # A-Z rail
    assert "Cocoa Beach" in body                          # city column + filter option
    assert 'name="city"' in body or 'href="/customers?city=' in body


def test_list_pagination_controls(client, engine):
    for i in range(60):
        _mk_customer(client, engine, f"Pager {i:03d}")
    body = client.get("/customers").text
    assert "page=2" in body                               # Next link present


def test_detail_map_absent_without_key(client, engine):
    cid = _mk_customer(client, engine, "NoMap Co", city="Cocoa Beach")
    body = client.get(f"/customers/{cid}").text
    assert "maps.geoapify.com" not in body


def test_csp_allows_geoapify_images(client):
    r = client.get("/customers")
    assert "maps.geoapify.com" in r.headers["content-security-policy"]


def test_detail_shows_map_and_navigate_with_key_and_coords(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import models
    cid = _mk_customer(client, engine, "Map Co", city="Cocoa Beach")
    with Session(engine) as s:
        models.set_setting(s, "geoapify_key", "TESTKEY")
        p = s.scalars(select(models.Property).where(models.Property.customer_id == cid)).one()
        p.latitude, p.longitude = 28.3, -80.6
        s.commit()
    body = client.get(f"/customers/{cid}").text
    assert "maps.geoapify.com" in body            # static map img rendered
    assert "maps.apple.com/?q=" in body           # Navigate deep link present


def test_pool_panel_shows_contract_and_new_contract_form(client, engine):
    cid = _mk_customer(client, engine, "Contract Co", city="Cocoa Beach")
    with Session(engine) as s:
        prop = s.scalars(select(models.Property).where(
            models.Property.customer_id == cid)).one()
        pool = models.Pool(property_id=prop.id, label="Main pool", kind="pool",
                           gallons=12000, active=True)
        s.add(pool)
        s.commit()
        pool_id = pool.id

    r = client.post(f"/pools/{pool_id}/contracts/new", follow_redirects=False)
    assert r.status_code == 303
    contract_id = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    with Session(engine) as s:
        season_year = s.get(models.Contract, contract_id).season_year

    body = client.get(f"/customers/{cid}").text
    assert f'href="/contracts/{contract_id}"' in body   # link to the contract
    assert str(season_year) in body                     # season shown
    assert "draft" in body                               # status pill
    assert f'action="/pools/{pool_id}/contracts/new"' in body  # New contract form
    assert "New contract" in body


def test_detail_has_custab_tab_bar_and_panels(client, engine):
    cid = _mk_customer(client, engine, "Tabbed Co", city="Cocoa Beach")
    body = client.get(f"/customers/{cid}").text
    for name in ("profile", "photos", "pool"):
        assert f'data-custab-btn="{name}"' in body
        assert f'data-custab="{name}"' in body
    assert "Tabbed Co" in body       # customer name still present
    assert "1 A St" in body          # property address still present

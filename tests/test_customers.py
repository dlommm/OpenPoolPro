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
        "label": "Beach house", "address": "1 Ocean Dr", "gate_code": "1234",
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
        "label": "Home", "address": "1 Bay St", "gate_code": "", "dog_note": "",
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
        "label": "H", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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

    detail = client.get(f"/customers/{cid}").text
    assert "2026-01-15" in detail
    assert "2026-01-15 00:00" not in detail  # date-only: no fabricated midnight
    assert "2026-07-20 09:30" in detail      # localized, not raw UTC
    assert "2026-07-20 13:30" not in detail  # raw naive-UTC must not leak through


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
        "label": "Cabana", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
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

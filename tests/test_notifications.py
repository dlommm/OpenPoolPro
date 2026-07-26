from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed(engine, email="cust@example.com", notify_email=True, label="Home"):
    with Session(engine) as s:
        c = models.Customer(name="V", email=email, notify_email=notify_email)
        p = models.Property(customer=c, label=label, frequency="weekly", weekday=0)
        s.add(p)
        s.add(models.Pool(property=p, gallons=12000))
        s.commit()
        return p.id, c.id


def _open(client, pid, visit_date="2026-07-13"):
    client.get(f"/visit/{pid}/{visit_date}")


def _vid(engine):
    with Session(engine) as s:
        return s.scalars(select(models.Visit)).first().id


def test_opted_in_with_email_queues_one_row_with_subject_and_body(client, engine):
    pid, cid = seed(engine)
    _open(client, pid)
    vid = _vid(engine)
    r = client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "",
        "tasks": ["Skim surface", "Brush walls"], "notes": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.EmailOutbox)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "pending"
        assert row.to_addr == "cust@example.com"
        assert row.subject == "OpenPoolPro — service visit completed"
        assert row.customer_id == cid  # FK persists (Task 1 review follow-up)
        assert "OpenPoolPro" in row.body
        assert "Home" in row.body
        assert "July 13, 2026" in row.body
        assert "Skim surface" in row.body
        assert "Brush walls" in row.body
        assert "Technician: Owner" in row.body
        assert "$" not in row.body
        assert "cents" not in row.body.lower()


def test_body_omits_task_section_when_no_tasks_completed(client, engine):
    pid, _ = seed(engine)
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""})
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        body = row.body
        # A visit with nothing recorded reports only who came and when: every
        # optional section is omitted rather than rendered with an empty list.
        assert "OpenPoolPro" in body
        assert "Home — July 13, 2026" in body
        assert "Technician: Owner" in body
        for section in ("Water test:", "Chemicals added:", "Completed:", "Notes:"):
            assert section not in body, f"empty {section!r} section was rendered"


def test_honors_configured_business_name(client, engine):
    pid, _ = seed(engine)
    with Session(engine) as s:
        models.set_setting(s, "business_name", "Sunny Pools LLC")
        s.commit()
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""})
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.subject == "Sunny Pools LLC — service visit completed"
        assert row.body.startswith("Sunny Pools LLC\n")


def test_no_email_address_queues_no_row(client, engine):
    pid, _ = seed(engine, email="", notify_email=True)
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""})
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_notify_email_false_queues_no_row(client, engine):
    pid, _ = seed(engine, email="cust@example.com", notify_email=False)
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""})
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_skip_queues_no_row(client, engine):
    pid, _ = seed(engine)
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/skip", data={"reason": "gate locked"})
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_dismiss_queues_no_row(client, engine):
    pid, _ = seed(engine)
    client.post(f"/visit/{pid}/2026-07-13/dismiss")
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_double_post_identical_form_queues_one_row_per_post_without_error(client, engine):
    # Offline-replay double-POST case: replay re-posts the identical form.
    # Each POST re-runs the whole complete flow, so each queues its own row
    # (acceptable per spec) -- what matters is neither POST errors and each
    # adds exactly one new row.
    pid, _ = seed(engine)
    _open(client, pid)
    vid = _vid(engine)
    data = {"fc": "3.5", "ph": "7.6", "ta": "", "cya": "", "salt": "",
            "tasks": ["Skim surface"], "notes": "all good"}
    r1 = client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False)
    assert r1.status_code == 303
    with Session(engine) as s:
        assert len(s.scalars(select(models.EmailOutbox)).all()) == 1
    r2 = client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False)
    assert r2.status_code == 303
    with Session(engine) as s:
        assert len(s.scalars(select(models.EmailOutbox)).all()) == 2


def test_blank_business_name_setting_falls_back_in_subject(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "business_name", "")
        s.commit()
    pid, cid = seed(engine, email="blank@x.com")
    _open(client, pid)
    vid = _vid(engine)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": ""},
        follow_redirects=False)
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.subject == "OpenPoolPro — service visit completed"

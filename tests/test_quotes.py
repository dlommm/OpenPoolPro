from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _customer_with_property(engine, name="QuoteCo"):
    with Session(engine) as s:
        c = models.Customer(name=name)
        p = models.Property(customer=c, label="Home")
        s.add(p)
        s.commit()
        return c.id, p.id


def test_quote_lifecycle_convert_creates_job_with_line_total(client, engine):
    cid, pid = _customer_with_property(engine)
    r = client.post("/quotes/new", data={
        "customer_id": str(cid), "property_id": str(pid), "title": "Resurface", "notes": "",
        "line_description": ["Labor", "Materials"],
        "line_amount_dollars": ["25.00", "75.00"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        q = s.scalars(select(models.Quote)).one()
        qid = q.id
        assert q.status == "draft"
        assert [l.amount_cents for l in q.lines] == [2500, 7500]

    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    client.post(f"/quotes/{qid}/status", data={"to": "accepted"})
    with Session(engine) as s:
        assert s.get(models.Quote, qid).status == "accepted"

    r = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    with Session(engine) as s:
        q = s.get(models.Quote, qid)
        assert q.status == "converted"
        assert q.job_id is not None
        job = s.get(models.Job, q.job_id)
        assert job.labor_cents == 10000
        assert job.job_type == "service"
        assert job.property_id == pid
        assert job.title == "Resurface"


def test_invalid_transition_rejected(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "T", "notes": "",
                                     "line_description": [""], "line_amount_dollars": [""]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    r = client.post(f"/quotes/{qid}/status", data={"to": "accepted"}, follow_redirects=False)
    assert r.status_code == 400
    r2 = client.post(f"/quotes/{qid}/status", data={"to": "sent"}, follow_redirects=False)
    assert r2.status_code == 303
    r3 = client.post(f"/quotes/{qid}/status", data={"to": "sent"}, follow_redirects=False)
    assert r3.status_code == 400  # already sent


def test_declined_quote_cannot_convert(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "T", "notes": "",
                                     "line_description": ["Labor"], "line_amount_dollars": ["50.00"]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    client.post(f"/quotes/{qid}/status", data={"to": "declined"})
    r = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r.status_code == 400
    with Session(engine) as s:
        assert s.get(models.Quote, qid).status == "declined"


def test_double_convert_rejected(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "T", "notes": "",
                                     "line_description": ["Labor"], "line_amount_dollars": ["50.00"]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    client.post(f"/quotes/{qid}/status", data={"to": "accepted"})
    r1 = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r1.status_code == 303
    r2 = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r2.status_code == 400


def test_duplicate_quote_copies_lines_as_new_draft(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "Original", "notes": "note here",
                                     "line_description": ["A", "B"],
                                     "line_amount_dollars": ["10.00", "20.00"]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})

    r = client.post(f"/quotes/{qid}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        quotes = s.scalars(select(models.Quote).order_by(models.Quote.id)).all()
        assert len(quotes) == 2
        original, dup = quotes
        assert original.status == "sent"  # untouched
        assert dup.status == "draft"
        assert dup.title == "Original (copy)"
        assert [l.amount_cents for l in dup.lines] == [1000, 2000]
        assert [l.description for l in dup.lines] == ["A", "B"]
        assert dup.customer_id == cid and dup.property_id == pid


def test_blank_amount_row_skipped(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={
        "customer_id": str(cid), "property_id": str(pid), "title": "T", "notes": "",
        "line_description": ["Keep", "Drop", "AlsoKeep"],
        "line_amount_dollars": ["10.00", "", "5.00"]})
    with Session(engine) as s:
        q = s.scalars(select(models.Quote)).one()
        assert [l.description for l in q.lines] == ["Keep", "AlsoKeep"]
        assert [l.amount_cents for l in q.lines] == [1000, 500]


def test_convert_blocked_when_customer_has_no_property(client, engine):
    with Session(engine) as s:
        c = models.Customer(name="NoProp")
        s.add(c)
        s.commit()
        cid = c.id
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": "",
                                     "title": "Repair", "notes": "",
                                     "line_description": ["Labor"], "line_amount_dollars": ["50.00"]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    client.post(f"/quotes/{qid}/status", data={"to": "accepted"})
    r = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r.status_code == 400
    with Session(engine) as s:
        assert s.get(models.Quote, qid).status == "accepted"  # not converted


def test_convert_falls_back_to_customers_first_property(client, engine):
    cid, pid = _customer_with_property(engine, name="Fallback")
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": "",
                                     "title": "Repair", "notes": "",
                                     "line_description": ["Labor"], "line_amount_dollars": ["50.00"]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    client.post(f"/quotes/{qid}/status", data={"to": "accepted"})
    r = client.post(f"/quotes/{qid}/convert", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        q = s.get(models.Quote, qid)
        job = s.get(models.Job, q.job_id)
        assert job.property_id == pid


def test_quotes_register_lists_and_filters_by_status(client, engine):
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "Draft quote", "notes": "",
                                     "line_description": ["L"], "line_amount_dollars": ["12.34"]})
    html = client.get("/quotes").text
    assert "Draft quote" in html and "$12.34" in html and "QuoteCo" in html
    assert "Draft quote" in client.get("/quotes?status=draft").text
    assert "Draft quote" not in client.get("/quotes?status=sent").text
    assert "Draft quote" in client.get("/quotes?status=bogus").text  # tolerant: shows all


def test_new_quote_with_mismatched_property_saves_with_no_property(client, engine):
    cid, pid = _customer_with_property(engine, name="QuoteCo")
    other_cid, other_pid = _customer_with_property(engine, name="OtherCo")
    r = client.post("/quotes/new", data={
        "customer_id": str(cid), "property_id": str(other_pid), "title": "Mismatch", "notes": "",
        "line_description": ["Labor"], "line_amount_dollars": ["50.00"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        q = s.scalars(select(models.Quote)).one()
        assert q.customer_id == cid
        assert q.property_id is None  # other customer's property -> tolerant None, not saved


def test_quote_totals_visible_to_manage_customers_without_view_money(client, tech_client, engine):
    # Quotes are pre-billing documents: manage_customers is the gate for /quotes,
    # and totals must show regardless of view_money (unlike jobs/customers money views).
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "Resurface", "notes": "",
                                     "line_description": ["Labor", "Materials"],
                                     "line_amount_dollars": ["25.00", "75.00"]})
    t = tech_client(name="moneyless", manage_customers=True)  # no view_money
    html = t.get("/quotes").text
    assert "$100.00" in html


def test_insert_job_template_as_quote_line(client, engine):
    with Session(engine) as s:
        t = models.JobTemplate(title="Pump swap", labor_cents=15000)
        s.add(t)
        s.commit()
        tid = t.id
    cid, pid = _customer_with_property(engine)
    client.post("/quotes/new", data={"customer_id": str(cid), "property_id": str(pid),
                                     "title": "T", "notes": "",
                                     "line_description": [""], "line_amount_dollars": [""]})
    with Session(engine) as s:
        qid = s.scalars(select(models.Quote)).one().id

    r = client.post(f"/quotes/{qid}/lines/from-template", data={"template_id": str(tid)},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        q = s.get(models.Quote, qid)
        assert len(q.lines) == 1
        assert q.lines[0].description == "Pump swap"
        assert q.lines[0].amount_cents == 15000

    # Templates can't be inserted once the quote has left draft.
    client.post(f"/quotes/{qid}/status", data={"to": "sent"})
    r2 = client.post(f"/quotes/{qid}/lines/from-template", data={"template_id": str(tid)},
                     follow_redirects=False)
    assert r2.status_code == 400


def test_job_templates_crud(client, engine):
    r = client.post("/settings/job_templates", data={
        "template_id": [""], "title": ["Pump swap"], "description": ["Replace pump"],
        "job_type": ["repair"], "labor_dollars": ["150.00"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        t = s.scalars(select(models.JobTemplate)).one()
        assert t.title == "Pump swap" and t.job_type == "repair" and t.labor_cents == 15000
        tid = t.id

    client.post("/settings/job_templates", data={
        "template_id": [str(tid)], "title": ["Pump swap v2"], "description": ["desc"],
        "job_type": ["service"], "labor_dollars": ["200.00"]})
    with Session(engine) as s:
        t = s.get(models.JobTemplate, tid)
        assert t.title == "Pump swap v2" and t.job_type == "service" and t.labor_cents == 20000

    client.post("/settings/job_templates", data={
        "template_id": [str(tid)], "title": [""], "description": [""],
        "job_type": ["service"], "labor_dollars": [""]})
    with Session(engine) as s:
        assert s.get(models.JobTemplate, tid) is None


def test_job_new_prefills_from_template(client, engine):
    with Session(engine) as s:
        t = models.JobTemplate(title="Filter clean", description="DE grids",
                               job_type="repair", labor_cents=12500)
        s.add(t)
        s.commit()
        tid = t.id

    # No ?template=: the dropdown always lists every template, carrying its
    # data as data-* attributes for the client-side JS to apply on selection --
    # but nothing is pre-selected.
    html = client.get("/jobs/new").text
    assert 'data-title="Filter clean"' in html
    assert 'data-description="DE grids"' in html
    assert 'data-job_type="repair"' in html
    assert f'value="{tid}" selected' not in html

    # ?template=<id>: same dropdown, but that option comes back pre-selected
    # (new_job resolves ?template= to preselect it).
    html_sel = client.get(f"/jobs/new?template={tid}").text
    assert f'value="{tid}" selected' in html_sel
    assert 'data-title="Filter clean"' in html_sel

    r = client.get("/jobs/new?template=abc")  # garbage template id: tolerant, plain form
    assert r.status_code == 200
    assert 'data-title="Filter clean"' in r.text  # dropdown still lists templates

    r2 = client.get("/jobs/new")  # missing template: plain form
    assert r2.status_code == 200
    assert 'data-title="Filter clean"' in r2.text

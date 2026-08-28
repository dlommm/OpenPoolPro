from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed(engine):
    with Session(engine) as s:
        c = models.Customer(name="V")
        p = models.Property(customer=c, frequency="weekly", weekday=0)
        s.add(p)
        s.add(models.Pool(property=p, gallons=12000))
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.cost_per_unit_cents = 450
        s.commit()
        return p.id, chem.id


def test_get_creates_due_visit_idempotently(client, engine):
    pid, _ = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vs = s.scalars(select(models.Visit)).all()
        assert len(vs) == 1 and vs[0].status == "due"


def test_complete_freezes_rate_and_chem_costs(client, engine):
    pid, chem_id = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    r = client.post(f"/visit/{vid}/complete", data={
        "fc": "3.5", "ph": "7.6", "ta": "80", "cya": "", "salt": "",
        "tasks": ["Skim surface", "Brush walls"], "notes": "all good",
        "chem_id": [str(chem_id)], "chem_amount": ["2.5"]},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        assert v.status == "done" and v.rate_cents == 4500  # small tier
        assert v.fc == 3.5 and v.cya is None
        assert v.tasks_done == ["Skim surface", "Brush walls"]
        vc = s.scalars(select(models.VisitChemical)).one()
        assert vc.unit_cost_cents_frozen == 450
        assert vc.total_cost_cents == 1125  # 2.5 * 450
    # catalog price change must not rewrite history
    with Session(engine) as s:
        s.get(models.Chemical, chem_id).cost_per_unit_cents = 999
        s.commit()
        assert s.scalars(select(models.VisitChemical)).one().total_cost_cents == 1125


def test_skip_and_dismiss(client, engine):
    pid, _ = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/skip", data={"reason": "gate locked"})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.status == "skipped" and "gate locked" in v.notes
    client.post(f"/visit/{pid}/2026-07-13/dismiss")
    with Session(engine) as s:
        assert s.get(models.Visit, vid).resolved is True


def test_recomplete_preserves_frozen_chem_cost(client, engine):
    # C1 pin: re-editing a done visit (e.g. fixing a pH typo) must not wipe
    # or re-price its already-billed chemical lines, even if the catalog
    # price changed in the meantime.
    pid, chem_id = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "3.5", "ph": "7.6", "ta": "", "cya": "", "salt": "",
        "notes": "", "chem_id": [str(chem_id)], "chem_amount": ["2.5"]})
    with Session(engine) as s:
        vc = s.scalars(select(models.VisitChemical)).one()
        assert vc.unit_cost_cents_frozen == 450 and vc.total_cost_cents == 1125
        # catalog price bumps after the visit was completed
        s.get(models.Chemical, chem_id).cost_per_unit_cents = 999
        s.commit()
    # Re-GET the form: the previously-submitted amount must be pre-filled.
    html = client.get(f"/visit/{pid}/2026-07-13").text
    assert 'value="2.5"' in html
    # Re-complete with the same amount (simulating a pH-typo fix elsewhere
    # on the form) — the chem line must keep its OLD frozen unit cost.
    client.post(f"/visit/{vid}/complete", data={
        "fc": "3.6", "ph": "7.6", "ta": "", "cya": "", "salt": "",
        "notes": "", "chem_id": [str(chem_id)], "chem_amount": ["2.5"]})
    with Session(engine) as s:
        vcs = s.scalars(select(models.VisitChemical)).all()
        assert len(vcs) == 1
        assert vcs[0].unit_cost_cents_frozen == 450  # old frozen, not 999
        assert vcs[0].total_cost_cents == 1125


def test_recomplete_new_chem_line_freezes_current_cost(client, engine):
    # C1 pin: a genuinely new line (chemical not previously on the visit)
    # must still freeze the CURRENT catalog cost, not reuse an unrelated
    # prior frozen cost.
    pid, chem_id = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
        other = models.Chemical(name="Muriatic acid", unit="gal", cost_per_unit_cents=300)
        s.add(other)
        s.commit()
        other_id = other.id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "",
        "notes": "", "chem_id": [str(chem_id)], "chem_amount": ["1.0"]})
    with Session(engine) as s:
        s.get(models.Chemical, other_id).cost_per_unit_cents = 350
        s.commit()
    # Re-complete (form re-rendered against the done visit, so it carries
    # form_status="done") adding a brand-new line for `other`.
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "",
        "notes": "", "form_status": "done",
        "chem_id": [str(chem_id), str(other_id)],
        "chem_amount": ["1.0", "2.0"]})
    with Session(engine) as s:
        by_chem = {vc.chemical_id: vc for vc in s.scalars(select(models.VisitChemical)).all()}
        assert by_chem[chem_id].unit_cost_cents_frozen == 450       # old line kept old frozen
        assert by_chem[other_id].unit_cost_cents_frozen == 350      # new line froze current cost
        assert by_chem[other_id].total_cost_cents == 700


def test_negative_and_garbage_chem_lines_ignored(client, engine):
    pid, chem_id = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    r = client.post(f"/visit/{vid}/complete", data={
        "fc": "inf", "ph": "", "ta": "", "cya": "", "salt": "",
        "notes": "", "chem_id": [str(chem_id), "zzz", str(chem_id), str(chem_id)],
        "chem_amount": ["-2.5", "1.0", "inf", "nan"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        assert v.status == "done" and v.fc is None
        assert s.scalars(select(models.VisitChemical)).all() == []


def test_visit_autostamp_on_due_day_only(client, engine):
    from datetime import timedelta
    pid, _ = seed(engine)
    past = "2026-07-13"
    client.get(f"/visit/{pid}/{past}")
    with Session(engine) as s:
        v = s.scalars(select(models.Visit).where(
            models.Visit.date == date(2026, 7, 13))).one()
        first_stamp = v.started_at
        assert first_stamp is not None  # past due date opened -> stamped
    client.get(f"/visit/{pid}/{past}")
    with Session(engine) as s:
        v = s.scalars(select(models.Visit).where(
            models.Visit.date == date(2026, 7, 13))).one()
        assert v.started_at == first_stamp  # no re-stamp
    future = (date.today() + timedelta(days=14)).isoformat()
    client.get(f"/visit/{pid}/{future}")
    with Session(engine) as s:
        vf = s.scalars(select(models.Visit)).all()[-1]
        assert vf.started_at is None  # future open -> no stamp


def test_double_complete_identical_form_pins_stock_cost_and_email(client, engine):
    # Replay pin: sw.js's offline queue replays a queued visit-complete POST by
    # re-sending the IDENTICAL urlencoded body — which carries the visit's
    # status as of form render ("due", or no form_status field at all). The
    # visit is done by the time the duplicate lands, so the replay must 409 and
    # change NOTHING: frozen chem costs unchanged, exactly one VisitChemical
    # row, stock decremented net once, and exactly one notification row (the
    # duplicate email per replay was a real customer-facing bug — see
    # tests/test_notifications.py's
    # test_replayed_double_post_gets_409_and_emails_the_customer_once).
    pid, chem_id = seed(engine)
    with Session(engine) as s:
        c = s.get(models.Property, pid).customer
        c.email, c.notify_email = "cust@example.com", True
        chem = s.get(models.Chemical, chem_id)
        chem.stock_qty = 100.0
        s.commit()
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    data = {"fc": "3.5", "ph": "7.6", "ta": "", "cya": "", "salt": "",
            "tasks": ["Skim surface"], "notes": "all good",
            "chem_id": [str(chem_id)], "chem_amount": ["2.5"]}

    r1 = client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False)
    assert r1.status_code == 303
    with Session(engine) as s:
        vc = s.scalars(select(models.VisitChemical)).one()
        assert vc.unit_cost_cents_frozen == 450
        assert vc.total_cost_cents == 1125  # 2.5 * 450
        stock_after_first = s.get(models.Chemical, chem_id).stock_qty
        assert stock_after_first == 97.5  # 100 - 2.5
        assert len(s.scalars(select(models.EmailOutbox)).all()) == 1

    # Catalog price bumps between the two (identical) POSTs -- must not leak
    # into the already-frozen line on replay.
    with Session(engine) as s:
        s.get(models.Chemical, chem_id).cost_per_unit_cents = 999
        s.commit()

    # Replay: the exact same urlencoded body, posted again — rejected whole.
    r2 = client.post(f"/visit/{vid}/complete", data=data, follow_redirects=False)
    assert r2.status_code == 409
    with Session(engine) as s:
        vcs = s.scalars(select(models.VisitChemical)).all()
        assert len(vcs) == 1  # exactly one VisitChemical row set, not two
        assert vcs[0].unit_cost_cents_frozen == 450  # frozen cost unchanged
        assert vcs[0].total_cost_cents == 1125
        stock_after_second = s.get(models.Chemical, chem_id).stock_qty
        assert stock_after_second == stock_after_first == 97.5
        # The homeowner got exactly ONE "visit completed" email, not one per replay.
        assert len(s.scalars(select(models.EmailOutbox)).all()) == 1


def test_stale_offline_replay_cannot_overwrite_a_skip(client, engine):
    # A completion queued offline Monday and replayed Wednesday — after the
    # office skipped the stop with a reason — must not overwrite the skip.
    # Before the form_status guard this was silent last-writer-wins: readings,
    # notes, and completed_by all replaced with no trace.
    pid, _ = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/skip", data={"reason": "gate locked"})
    r = client.post(f"/visit/{vid}/complete", data={
        "fc": "3.5", "ph": "7.6", "ta": "", "cya": "", "salt": "",
        "notes": "queued while offline"}, follow_redirects=False)
    assert r.status_code == 409
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.status == "skipped"
        assert "gate locked" in (v.notes or "")
        assert v.fc is None  # the stale readings were not applied


def test_complete_with_edited_times(client, engine):
    pid, _ = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "started_hhmm": "09:15", "ended_hhmm": "09:57"})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.duration_minutes == 42
    # garbage keeps stamped values (re-complete)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "started_hhmm": "9:99", "ended_hhmm": "abc"})
    with Session(engine) as s:
        assert s.get(models.Visit, vid).duration_minutes == 42


def test_late_completion_keeps_real_duration(client, engine):
    from app.timefmt import local_date, to_local_hhmm
    pid, _ = seed(engine)
    client.get(f"/visit/{pid}/2026-07-13")  # past due date -> autostamp = real now
    with Session(engine) as s:
        v = s.scalars(select(models.Visit)).one()
        vid, prefill = v.id, to_local_hhmm(v.started_at)
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "started_hhmm": prefill, "ended_hhmm": ""})
    with Session(engine) as s:
        v = s.get(models.Visit, vid)
        assert v.duration_minutes is not None and v.duration_minutes < 60
        assert local_date(v.started_at) == local_date(v.completed_at)

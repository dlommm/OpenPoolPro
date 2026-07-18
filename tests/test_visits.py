from datetime import date

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
    # Re-complete adding a brand-new line for `other` alongside the existing one.
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "",
        "notes": "", "chem_id": [str(chem_id), str(other_id)],
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

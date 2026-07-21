from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed_visit_chem(engine, stock_qty=10.0, reorder_point=None):
    """Property + pool (so a visit can be logged) plus the seeded 'Liquid
    chlorine' chemical primed with a known stock level."""
    with Session(engine) as s:
        c = models.Customer(name="V")
        p = models.Property(customer=c, frequency="weekly", weekday=0)
        s.add(p)
        s.add(models.Pool(property=p, gallons=12000))
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.cost_per_unit_cents = 450
        chem.stock_qty = stock_qty
        chem.reorder_point = reorder_point
        s.commit()
        return p.id, chem.id


def seed_chem(engine, stock_qty=10.0, reorder_point=None):
    """Just prime the seeded 'Liquid chlorine' chemical; no visit needed."""
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.cost_per_unit_cents = 450
        chem.stock_qty = stock_qty
        chem.reorder_point = reorder_point
        s.commit()
        return chem.id


# --- visit-save stock wiring --------------------------------------------

def test_visit_complete_decrements_stock_for_two_lines(client, engine):
    pid, chem_id = seed_visit_chem(engine, stock_qty=10.0)
    with Session(engine) as s:
        other = models.Chemical(name="Muriatic acid", unit="gal",
                                cost_per_unit_cents=300, stock_qty=10.0)
        s.add(other)
        s.commit()
        other_id = other.id
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "chem_id": [str(chem_id), str(other_id)], "chem_amount": ["2.5", "1.0"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 7.5
        assert s.get(models.Chemical, other_id).stock_qty == 9.0


def test_visit_edit_raising_amount_decrements_only_difference(client, engine):
    pid, chem_id = seed_visit_chem(engine, stock_qty=10.0)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "chem_id": [str(chem_id)], "chem_amount": ["1.0"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 9.0
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "chem_id": [str(chem_id)], "chem_amount": ["1.5"]})
    with Session(engine) as s:
        # 1.0 -> 1.5 is a +0.5 delta: only the extra 0.5 comes off stock
        assert s.get(models.Chemical, chem_id).stock_qty == 8.5


def test_visit_edit_removing_line_restores_stock(client, engine):
    pid, chem_id = seed_visit_chem(engine, stock_qty=10.0)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "chem_id": [str(chem_id)], "chem_amount": ["2.0"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 8.0
    client.post(f"/visit/{vid}/complete", data={
        "fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
        "chem_id": [], "chem_amount": []})
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 10.0  # fully restored


def test_visit_resave_identical_is_net_zero(client, engine):
    pid, chem_id = seed_visit_chem(engine, stock_qty=10.0)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    data = {"fc": "", "ph": "", "ta": "", "cya": "", "salt": "", "notes": "",
            "chem_id": [str(chem_id)], "chem_amount": ["2.0"]}
    client.post(f"/visit/{vid}/complete", data=data)
    client.post(f"/visit/{vid}/complete", data=data)
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 8.0


def test_skip_with_no_lines_no_stock_change(client, engine):
    pid, chem_id = seed_visit_chem(engine, stock_qty=10.0)
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/visit/{vid}/skip", data={"reason": "no access"})
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 10.0


# --- /inventory page + purchase/adjust posts ----------------------------

def test_purchase_updates_stock_and_cost(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "3", "cost_dollars": "10.00",
        "update_cost": "on", "note": "restock"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.get(models.Chemical, chem_id)
        assert c.stock_qty == 8.0
        assert c.cost_per_unit_cents == round(1000 / 3) == 333
        purchase = s.scalars(select(models.Purchase)).one()
        assert purchase.note == "restock"


def test_inventory_admin_only_sweep(client, tech_client, engine):
    chem_id = seed_chem(engine)
    t = tech_client(name="bare_inv")
    assert t.get("/inventory", follow_redirects=False).status_code == 403
    assert t.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "1", "cost_dollars": "1"},
        follow_redirects=False).status_code == 403
    assert t.post(f"/inventory/adjust/{chem_id}", data={"target": "1"},
                  follow_redirects=False).status_code == 403
    assert t.post(f"/inventory/reorder/{chem_id}", data={"reorder_point": "1"},
                  follow_redirects=False).status_code == 403


def test_purchase_tolerant_bad_float_no_mutation(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "abc", "cost_dollars": "10",
        "note": "x"})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


def test_purchase_rejects_non_finite_quantity(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "inf", "cost_dollars": "10"})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


def test_adjust_sets_exact_and_logs_history(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post(f"/inventory/adjust/{chem_id}", data={"target": "2.5", "note": "recount"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.get(models.Chemical, chem_id)
        assert c.stock_qty == 2.5
        p = s.scalars(select(models.Purchase)).one()
        assert p.quantity == -2.5 and p.total_cost_cents == 0
        assert p.note == "adjust: recount"


def test_adjust_tolerant_bad_float_no_mutation(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post(f"/inventory/adjust/{chem_id}", data={"target": "nan", "note": "x"})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


def test_dashboard_low_stock_card_count(client, engine):
    seed_chem(engine, stock_qty=2.0, reorder_point=5.0)
    html = client.get("/dashboard").text
    assert "Low stock" in html
    assert "Liquid chlorine" in html
    # exactly one chemical is low: the card's count digit must render "1",
    # not just contain the word "Low stock" somewhere on the page.
    assert '<span class="font-data text-sm text-safety">1</span>' in html


def test_nav_badge_shows_low_stock_count(client, engine):
    seed_chem(engine, stock_qty=2.0, reorder_point=5.0)
    html = client.get("/inventory").text
    assert '<span class="tag tag-safety ml-auto">1</span>' in html


def test_nav_badge_absent_when_no_low_stock(client, engine):
    seed_chem(engine, stock_qty=10.0, reorder_point=5.0)
    html = client.get("/inventory").text
    assert "tag tag-safety ml-auto" not in html


def test_inventory_low_stock_row_highlight(client, engine):
    seed_chem(engine, stock_qty=2.0, reorder_point=5.0)
    html = client.get("/inventory").text
    assert "bg-safety/5" in html


def test_inventory_history_and_negative_stock_styling(client, engine):
    chem_id = seed_chem(engine, stock_qty=1.0)
    client.post(f"/inventory/adjust/{chem_id}", data={"target": "-3", "note": "shrinkage"})
    html = client.get("/inventory").text
    assert "text-safety" in html  # negative on-hand rendered with warning styling
    assert "shrinkage" in html


# --- reorder point/qty editor -------------------------------------------

def test_reorder_post_stores_values_and_triggers_low_stock_highlight(client, engine):
    chem_id = seed_chem(engine, stock_qty=3.0)
    r = client.post(f"/inventory/reorder/{chem_id}", data={
        "reorder_point": "5", "reorder_qty": "10"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.get(models.Chemical, chem_id)
        assert c.reorder_point == 5.0
        assert c.reorder_qty == 10.0
    html = client.get("/inventory").text
    assert "bg-safety/5" in html  # stock 3 <= reorder point 5 -> low-stock highlight


def test_reorder_blank_clears_to_none(client, engine):
    chem_id = seed_chem(engine, stock_qty=3.0, reorder_point=5.0)
    with Session(engine) as s:
        s.get(models.Chemical, chem_id).reorder_qty = 10.0
        s.commit()
    r = client.post(f"/inventory/reorder/{chem_id}", data={
        "reorder_point": "", "reorder_qty": ""}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.get(models.Chemical, chem_id)
        assert c.reorder_point is None  # alerting off
        assert c.reorder_qty is None


def test_reorder_garbage_input_no_mutation(client, engine):
    chem_id = seed_chem(engine, stock_qty=3.0, reorder_point=5.0)
    r = client.post(f"/inventory/reorder/{chem_id}", data={
        "reorder_point": "abc", "reorder_qty": "10"})
    assert r.status_code == 200
    with Session(engine) as s:
        c = s.get(models.Chemical, chem_id)
        assert c.reorder_point == 5.0  # unchanged
        assert c.reorder_qty is None  # unchanged


def test_reorder_nonfinite_input_no_mutation(client, engine):
    chem_id = seed_chem(engine, stock_qty=3.0, reorder_point=5.0)
    r = client.post(f"/inventory/reorder/{chem_id}", data={
        "reorder_point": "nan", "reorder_qty": ""})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).reorder_point == 5.0  # unchanged


# --- last-purchase column excludes recount adjustments -------------------

def test_last_purchase_excludes_adjustment_rows(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    client.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "3", "cost_dollars": "9.00",
        "update_cost": "on", "note": "restock"})
    client.post(f"/inventory/adjust/{chem_id}", data={"target": "100", "note": "recount"})
    html = client.get("/inventory").text
    # the recount adjustment (note "adjust: recount", $0.00/unit) must not
    # displace the real purchase's $3.00/unit in the last-purchase column
    assert '<span class="money">$3.00</span>/gal' in html


# --- badge guard exception branch ----------------------------------------

def test_badge_guard_swallows_exception_and_page_still_renders(client, engine, monkeypatch):
    def boom(session):
        raise RuntimeError("boom")
    monkeypatch.setattr("app.inventory.low_stock", boom)
    r = client.get("/customers")
    assert r.status_code == 200
    assert "tag tag-safety ml-auto" not in r.text


# --- zero-delta set-on-hand writes no noise row ---------------------------

def test_adjust_zero_delta_no_purchase_row(client, engine):
    chem_id = seed_chem(engine, stock_qty=5.0)
    r = client.post(f"/inventory/adjust/{chem_id}", data={"target": "5.0", "note": "recount"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Chemical, chem_id).stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


# --- _context enrichment (value/counts/split/restock) ---------------------

from app.routers.inventory import _context  # noqa: E402


def test_context_splits_purchases_and_adjustments(client, engine):
    chem_id = seed_chem(engine, stock_qty=10.0)
    client.post("/inventory/purchase", data={
        "chemical_id": str(chem_id), "quantity": "5", "cost_dollars": "20",
        "update_cost": "on", "note": "restock run"})
    client.post(f"/inventory/adjust/{chem_id}", data={"target": "12", "note": "recount"})
    with Session(engine) as s:
        ctx = _context(s)
    pnotes = [p.note for p in ctx["purchases"]]
    anotes = [p.note for p in ctx["adjustments"]]
    assert "restock run" in pnotes
    assert any(n.startswith("adjust: ") for n in anotes)
    assert all(not n.startswith("adjust: ") for n in pnotes)


def test_context_reports_counts_and_value(client, engine):
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.cost_per_unit_cents = 450
        chem.stock_qty = 4.0
        chem.reorder_point = 5.0    # low (4 <= 5)
        s.commit()
        ctx = _context(s)
    assert isinstance(ctx["total_value_cents"], int)
    assert ctx["low_count"] >= 1
    assert isinstance(ctx["out_count"], int)
    assert ctx["item_count"] >= 1
    assert any(r["chemical"].name == "Liquid chlorine" for r in ctx["reorder_items"])


def test_context_restock_prefill(client, engine):
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        chem.reorder_qty = 15.0
        s.commit()
        cid = chem.id
        ctx = _context(s, restock_id=cid)
    assert ctx["restock_id"] == cid
    assert ctx["restock_qty"] == 15.0

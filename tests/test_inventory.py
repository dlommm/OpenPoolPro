import math

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inventory, models
from app.db import Base


def _chem(s, **kw):
    defaults = dict(name="Liquid chlorine", unit="gal", cost_per_unit_cents=100,
                    active=True, stock_qty=10.0)
    defaults.update(kw)
    c = models.Chemical(**defaults)
    s.add(c)
    s.commit()
    return c


# --- apply_visit_delta ------------------------------------------------

def test_delta_new_line_decrements_full_amount(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=10.0)
        inventory.apply_visit_delta(s, 1, {}, {c.id: 4.0})
        assert c.stock_qty == 6.0


def test_delta_increase_only_decrements_the_difference(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=10.0)
        inventory.apply_visit_delta(s, 1, {c.id: 4.0}, {c.id: 6.0})
        assert c.stock_qty == 8.0  # only the +2 difference consumed


def test_delta_removed_line_restores_stock(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=10.0)
        inventory.apply_visit_delta(s, 1, {c.id: 4.0}, {})
        assert c.stock_qty == 14.0  # fully restored


def test_delta_identical_resave_is_net_zero(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=10.0)
        inventory.apply_visit_delta(s, 1, {c.id: 4.0}, {c.id: 4.0})
        assert c.stock_qty == 10.0


def test_delta_union_of_keys_multiple_chemicals(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a = _chem(s, name="A", stock_qty=10.0)
        b = _chem(s, name="B", stock_qty=10.0)
        # a: removed entirely, b: brand new
        inventory.apply_visit_delta(s, 1, {a.id: 3.0}, {b.id: 5.0})
        assert a.stock_qty == 13.0
        assert b.stock_qty == 5.0


def test_delta_allows_negative_stock(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=2.0)
        inventory.apply_visit_delta(s, 1, {}, {c.id: 5.0})
        assert c.stock_qty == -3.0


# --- record_purchase ----------------------------------------------------

def test_record_purchase_happy_path(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=0.0, cost_per_unit_cents=999)
        p = inventory.record_purchase(s, c, 3, 1000, "restock", None)
        assert p is not None
        assert c.stock_qty == 3
        assert c.cost_per_unit_cents == round(1000 / 3) == 333
        assert p.quantity == 3
        assert p.total_cost_cents == 1000
        assert p.note == "restock"
        assert p.chemical_id == c.id


@pytest.mark.parametrize("bad_qty", [0, -1, float("inf"), float("nan")])
def test_record_purchase_rejects_bad_quantity(engine, bad_qty):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=5.0, cost_per_unit_cents=100)
        before_qty, before_cost = c.stock_qty, c.cost_per_unit_cents
        before_count = len(s.scalars(select(models.Purchase)).all())
        result = inventory.record_purchase(s, c, bad_qty, 1000, "bad", None)
        assert result is None
        assert c.stock_qty == before_qty
        assert c.cost_per_unit_cents == before_cost
        assert len(s.scalars(select(models.Purchase)).all()) == before_count


def test_record_purchase_rejects_negative_cost(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=5.0, cost_per_unit_cents=100)
        result = inventory.record_purchase(s, c, 2, -1, "bad", None)
        assert result is None
        assert c.stock_qty == 5.0
        assert c.cost_per_unit_cents == 100
        assert s.scalars(select(models.Purchase)).all() == []


def test_record_purchase_skips_cost_update_when_flag_false(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=0.0, cost_per_unit_cents=999)
        p = inventory.record_purchase(s, c, 3, 1000, "restock", None, update_cost=False)
        assert p is not None
        assert c.stock_qty == 3
        assert c.cost_per_unit_cents == 999  # unchanged


def test_record_purchase_allows_zero_cost_and_no_update_still_stocks(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=0.0, cost_per_unit_cents=50)
        p = inventory.record_purchase(s, c, 5, 0, "donation", None)
        assert p is not None
        assert c.stock_qty == 5
        assert c.cost_per_unit_cents == 0  # round(0/5)


# --- set_on_hand ----------------------------------------------------------

def test_set_on_hand_writes_exact_target_and_delta_row(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=5.0)
        p = inventory.set_on_hand(s, c, 2.5, "recount", None)
        assert p is not None
        assert c.stock_qty == 2.5
        assert p.quantity == -2.5
        assert p.total_cost_cents == 0
        assert p.note == "adjust: recount"


def test_set_on_hand_rejects_non_finite_target(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=5.0)
        result = inventory.set_on_hand(s, c, float("inf"), "recount", None)
        assert result is None
        assert c.stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


def test_set_on_hand_zero_delta_short_circuits_no_purchase_row(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, stock_qty=5.0)
        result = inventory.set_on_hand(s, c, 5.0, "recount", None)
        assert result is not None  # a no-op recount still reports success
        assert c.stock_qty == 5.0
        assert s.scalars(select(models.Purchase)).all() == []


# --- low_stock --------------------------------------------------------

def test_low_stock_boundary_equal_is_included(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, name="Boundary", stock_qty=5.0, reorder_point=5.0)
        assert c in inventory.low_stock(s)


def test_low_stock_excludes_null_reorder_point(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, name="NoAlert", stock_qty=0.0, reorder_point=None)
        assert c not in inventory.low_stock(s)


def test_low_stock_excludes_inactive(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, name="Inactive", stock_qty=0.0, reorder_point=10.0, active=False)
        assert c not in inventory.low_stock(s)


def test_low_stock_excludes_above_reorder_point(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, name="Plenty", stock_qty=20.0, reorder_point=10.0)
        assert c not in inventory.low_stock(s)


def test_low_stock_ordered_by_name(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="Zebra chem", stock_qty=1.0, reorder_point=5.0)
        _chem(s, name="Alpha chem", stock_qty=1.0, reorder_point=5.0)
        names = [c.name for c in inventory.low_stock(s)]
        assert names == sorted(names)


# --- inventory_value_cents --------------------------------------------

def test_value_empty_is_zero(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert inventory.inventory_value_cents(s) == 0


def test_value_sums_qty_times_cost(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="A", stock_qty=10.0, cost_per_unit_cents=100)   # 1000
        _chem(s, name="B", stock_qty=2.5, cost_per_unit_cents=400)    # 1000
        assert inventory.inventory_value_cents(s) == 2000


def test_value_counts_negative_stock(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="A", stock_qty=-3.0, cost_per_unit_cents=200)   # -600
        assert inventory.inventory_value_cents(s) == -600


def test_value_ignores_inactive(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="A", stock_qty=10.0, cost_per_unit_cents=100)   # 1000
        _chem(s, name="Z", stock_qty=99.0, cost_per_unit_cents=999, active=False)
        assert inventory.inventory_value_cents(s) == 1000


# --- reorder_list ------------------------------------------------------

def test_reorder_list_returns_low_items_with_suggested_qty(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        c = _chem(s, name="A", stock_qty=1.0, reorder_point=5.0, reorder_qty=15.0)
        rows = inventory.reorder_list(s)
        assert len(rows) == 1
        assert rows[0]["chemical"].id == c.id
        assert rows[0]["suggested_qty"] == 15.0


def test_reorder_list_suggested_qty_none_when_unset(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="A", stock_qty=1.0, reorder_point=5.0, reorder_qty=None)
        assert inventory.reorder_list(s)[0]["suggested_qty"] is None


def test_reorder_list_excludes_non_low(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="A", stock_qty=50.0, reorder_point=5.0, reorder_qty=15.0)  # above point
        _chem(s, name="B", stock_qty=99.0, reorder_point=None)                   # alerting off
        assert inventory.reorder_list(s) == []


def test_reorder_list_name_order(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _chem(s, name="Zinc", stock_qty=0.0, reorder_point=5.0)
        _chem(s, name="Acid", stock_qty=0.0, reorder_point=5.0)
        names = [r["chemical"].name for r in inventory.reorder_list(s)]
        assert names == ["Acid", "Zinc"]

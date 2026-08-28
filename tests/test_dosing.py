import pytest

from app.dosing import recommend


class Chem:
    """Stand-in with the attrs recommend() reads (id, name, unit, role, strength_pct, active)."""
    def __init__(self, id, name, unit, role, strength_pct, active=True):
        self.id, self.name, self.unit = id, name, unit
        self.role, self.strength_pct, self.active = role, strength_pct, active


CATALOG = [
    Chem(1, "Liquid chlorine", "gal", "chlorine_liquid", 12.5),
    Chem(2, "Shock (cal-hypo)", "lb", "chlorine_granular", 65),
    Chem(3, "Muriatic acid", "gal", "ph_down", 31.45),
    Chem(4, "Soda ash", "lb", "ph_up", 100),
    Chem(5, "Baking soda", "lb", "alkalinity_up", 100),
    Chem(6, "Stabilizer (CYA)", "lb", "stabilizer", 100),
    Chem(7, "Salt", "lb", "salt", 100),
]
TARGETS = {"fc": 4.0, "ph": 7.5, "ta": 90.0, "cya": 50.0, "salt": 3200.0}


def rec(readings, gallons=16875, water_type="chlorine", catalog=CATALOG):
    return recommend(readings, gallons, water_type, catalog, TARGETS)


def by_role(doses, name_part):
    return next((d for d in doses if name_part in d.chemical_name), None)


def test_fc_deficit_liquid_preferred():
    doses = rec({"fc": 2.0})
    d = by_role(doses, "Liquid chlorine")
    # pure lbs = 2 * 16875 * 8.34 / 1e6 = 0.2815; /0.125 = 2.252 lb; /10 = 0.225 gal -> 0.2
    assert d is not None and d.unit == "gal" and d.amount == 0.2 and d.applyable


def test_fc_granular_fallback():
    catalog = [c for c in CATALOG if c.role != "chlorine_liquid"]
    d = by_role(rec({"fc": 2.0}, catalog=catalog), "Shock")
    # 0.2815 / 0.65 = 0.433 lb -> 0.5 (0.25 increment, round-half-up regions ok)
    assert d is not None and d.unit == "lb" and d.amount == 0.5


def test_fc_within_deadband_no_dose():
    assert rec({"fc": 3.6}) == []          # 4.0 - 0.5 = 3.5 threshold; 3.6 above it
    assert rec({"fc": 3.5}) == []          # exactly at threshold: no dose


def test_ph_down_with_ta_scaling_and_cap():
    doses = rec({"ph": 8.0, "ta": 100.0})
    acid = by_role(doses, "Muriatic")
    # raw = (0.5/0.2)*26*1.6875*1.0 = 109.7 fl oz -> capped at 32*1.6875 = 54 fl oz
    # -> 54/128 = 0.422 gal -> 0.4 gal, note about re-test
    assert acid is not None and acid.unit == "gal" and acid.amount == 0.4
    assert "re-test" in acid.note


def test_ph_down_without_ta_reading_uses_factor_1():
    acid = by_role(rec({"ph": 7.9}), "Muriatic")
    # (0.4/0.2)*26*1.6875 = 87.75 -> capped 54 -> 0.4 gal
    assert acid is not None and acid.amount == 0.4


def test_ph_in_band_no_dose():
    assert rec({"ph": 7.7}) == []
    assert rec({"ph": 7.2}) == []


def test_ph_up():
    d = by_role(rec({"ph": 7.0}), "Soda ash")
    # (0.5/0.2)*6*1.6875 = 25.3 oz -> /16 = 1.58 lb -> 1.5 lb
    assert d is not None and d.unit == "lb" and d.amount == 1.5


def test_ta_raise():
    d = by_role(rec({"ta": 70.0}), "Baking soda")
    # (20/10)*1.5*1.6875 = 5.06 lb -> 5.0
    assert d is not None and d.amount == 5.0


def test_cya_raise_and_high_warning():
    d = by_role(rec({"cya": 30.0}), "Stabilizer")
    # 20*1.6875/12 = 2.8125 -> 2.75
    assert d is not None and d.amount == 2.75
    warn = rec({"cya": 120.0})
    assert len(warn) == 1 and not warn[0].applyable and "dilution" in warn[0].note


def test_salt_only_for_salt_pools():
    assert rec({"salt": 2800.0}) == []  # chlorine pool
    d = by_role(recommend({"salt": 2800.0}, 15000, "salt", CATALOG, TARGETS), "Salt")
    # 400 * 15000 * 8.34 / 1e6 = 50.04 lb -> 0.25 lb increment -> 50.0
    assert d is not None and d.amount == 50.0


def test_missing_role_advisory():
    catalog = [c for c in CATALOG if not c.role.startswith("chlorine")]
    doses = rec({"fc": 2.0}, catalog=catalog)
    assert len(doses) == 1 and doses[0].chemical_id is None
    assert not doses[0].applyable and "no chlorine" in doses[0].note


def test_inactive_chemicals_skipped():
    catalog = [Chem(1, "Liquid chlorine", "gal", "chlorine_liquid", 12.5, active=False),
               Chem(2, "Shock (cal-hypo)", "lb", "chlorine_granular", 65)]
    d = by_role(rec({"fc": 2.0}, catalog=catalog), "Shock")
    assert d is not None


def test_unconvertible_unit_not_applyable():
    catalog = [Chem(1, "Chlorine pucks", "tab", "chlorine_liquid", 12.5)]
    doses = rec({"fc": 2.0}, catalog=catalog)
    assert len(doses) == 1 and not doses[0].applyable
    assert "dose manually" in doses[0].note


@pytest.mark.parametrize("readings,role_word", [
    ({"ph": 8.0}, "ph_down"),
    ({"ph": 7.0}, "ph_up"),
    ({"ta": 70.0}, "alkalinity_up"),
    ({"cya": 30.0}, "stabilizer"),
])
def test_missing_role_advisories(readings, role_word):
    doses = recommend(readings, 16875, "chlorine", [], TARGETS)
    assert len(doses) == 1 and doses[0].chemical_id is None
    assert role_word in doses[0].note


def test_missing_salt_role_advisory():
    doses = recommend({"salt": 2800.0}, 15000, "salt", [], TARGETS)
    assert len(doses) == 1 and "salt" in doses[0].note


def test_no_readings_no_doses():
    assert rec({}) == []
    assert rec({"fc": None, "ph": None}) == []


def test_tiny_dose_rounds_to_zero_dropped():
    assert rec({"fc": 3.4}, gallons=800) == []  # deficit 0.6 on tiny pool -> < 0.05 gal


def test_zero_strength_chemical_skipped():
    catalog = [Chem(1, "Liquid chlorine", "gal", "chlorine_liquid", 0),
               Chem(2, "Shock (cal-hypo)", "lb", "chlorine_granular", 65)]
    d = by_role(rec({"fc": 2.0}, catalog=catalog), "Shock")
    assert d is not None  # zero-strength liquid skipped, granular used


def test_all_strengths_unusable_advisory():
    catalog = [Chem(1, "Liquid chlorine", "gal", "chlorine_liquid", 0),
               Chem(2, "Shock (cal-hypo)", "lb", "chlorine_granular", -5)]
    doses = rec({"fc": 2.0}, catalog=catalog)
    assert len(doses) == 1 and doses[0].chemical_id is None and not doses[0].applyable


def test_negative_readings_ignored():
    assert rec({"fc": -2.0}) == []
    assert rec({"cya": -100.0}) == []


def test_negative_ta_does_not_corrupt_acid_dose():
    doses = rec({"ph": 8.2, "ta": -50.0})
    acid = by_role(doses, "Muriatic")
    assert acid is not None and acid.amount > 0  # ta treated absent -> factor 1.0
    assert by_role(doses, "Baking soda") is None  # no phantom TA-raise advice

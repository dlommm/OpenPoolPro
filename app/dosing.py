"""Target-based dose recommendations. Pure and deterministic.

Nothing is dosed inside the band around each target (app/chem_targets.py,
the same band the alerts and charts use): FC only below target-band; pH only
outside target±band; TA and CYA below target-band (CYA raise only, >100
warns); salt below target-band on salt pools. Doses rounding to zero are
dropped.
"""
import math
from dataclasses import dataclass

from app.chem_targets import DEFAULT_BANDS

CYA_HIGH = 100.0
LIQUID_LB_PER_GAL = 10.0
ACID_CAP_FLOZ_PER_10K = 32.0

ROUND_INCREMENT = {"gal": 0.1, "lb": 0.25, "oz": 1.0}
CONVERT = {("fl_oz", "gal"): 1 / 128, ("fl_oz", "fl_oz"): 1.0,
           ("oz", "lb"): 1 / 16, ("oz", "oz"): 1.0,
           ("gal", "gal"): 1.0, ("lb", "lb"): 1.0}


@dataclass
class Dose:
    chemical_id: int | None
    chemical_name: str
    amount: float
    unit: str
    note: str = ""
    applyable: bool = True


def _first(chemicals, role):
    for c in sorted(chemicals, key=lambda c: c.name):
        if c.active and c.role == role and math.isfinite(c.strength_pct) and c.strength_pct > 0:
            return c
    return None


def _round_amount(amount: float, unit: str) -> float:
    inc = ROUND_INCREMENT.get(unit, 0.1)
    return round(round(amount / inc) * inc, 2)


def _emit(out, chem, computed_amount, computed_unit, note=""):
    if chem is None:
        return
    factor = CONVERT.get((computed_unit, chem.unit))
    if factor is None:
        amount = _round_amount(computed_amount, computed_unit)
        if amount > 0:
            out.append(Dose(chem.id, chem.name, amount, computed_unit,
                            note=note or f"can't convert to {chem.unit} — dose manually",
                            applyable=False))
        return
    amount = _round_amount(computed_amount * factor, chem.unit)
    if amount > 0:
        out.append(Dose(chem.id, chem.name, amount, chem.unit, note=note))


def recommend(readings, gallons, water_type, chemicals, targets, bands=None):
    band = {**DEFAULT_BANDS, **(bands or {})}
    out: list[Dose] = []
    if not gallons or gallons <= 0:
        return out
    g10k = gallons / 10_000.0

    def r(key):
        v = readings.get(key)
        if v is None:
            return None
        v = float(v)
        return v if v >= 0 else None

    fc, ph, ta, cya, salt = r("fc"), r("ph"), r("ta"), r("cya"), r("salt")

    if fc is not None and fc < targets["fc"] - band["fc"]:
        deficit = targets["fc"] - fc
        pure_lbs = deficit * gallons * 8.34 / 1_000_000
        chem = _first(chemicals, "chlorine_liquid")
        if chem is not None:
            product_lbs = pure_lbs / (chem.strength_pct / 100.0)
            _emit(out, chem, product_lbs / LIQUID_LB_PER_GAL, "gal")
        else:
            chem = _first(chemicals, "chlorine_granular")
            if chem is not None:
                _emit(out, chem, pure_lbs / (chem.strength_pct / 100.0), "lb")
            else:
                out.append(Dose(None, "Chlorine", 0, "", applyable=False,
                                note="no chlorine chemical in catalog"))

    if ph is not None and ph > targets["ph"] + band["ph"]:
        chem = _first(chemicals, "ph_down")
        if chem is not None:
            ta_factor = (ta / 100.0) if ta is not None else 1.0
            floz = ((ph - targets["ph"]) / 0.2) * 26.0 * g10k * ta_factor \
                * (31.45 / chem.strength_pct)
            cap = ACID_CAP_FLOZ_PER_10K * g10k
            note = ""
            if floz > cap:
                floz = cap
                note = "capped — re-test before adding more"
            _emit(out, chem, floz, "fl_oz", note=note)
        else:
            out.append(Dose(None, "pH down", 0, "", applyable=False,
                            note="no ph_down chemical in catalog"))
    elif ph is not None and ph < targets["ph"] - band["ph"]:
        chem = _first(chemicals, "ph_up")
        if chem is not None:
            oz = ((targets["ph"] - ph) / 0.2) * 6.0 * g10k * (100.0 / chem.strength_pct)
            _emit(out, chem, oz, "oz")
        else:
            out.append(Dose(None, "pH up", 0, "", applyable=False,
                            note="no ph_up chemical in catalog"))

    if ta is not None and ta < targets["ta"] - band["ta"]:
        chem = _first(chemicals, "alkalinity_up")
        if chem is not None:
            lbs = ((targets["ta"] - ta) / 10.0) * 1.5 * g10k
            _emit(out, chem, lbs, "lb")
        else:
            out.append(Dose(None, "Alkalinity up", 0, "", applyable=False,
                            note="no alkalinity_up chemical in catalog"))

    if cya is not None:
        if cya > CYA_HIGH:
            out.append(Dose(None, "Stabilizer", 0, "", applyable=False,
                            note="CYA high — partial drain/dilution recommended"))
        elif cya < targets["cya"] - band["cya"]:
            chem = _first(chemicals, "stabilizer")
            if chem is not None:
                lbs = (targets["cya"] - cya) * g10k / 12.0
                _emit(out, chem, lbs, "lb")
            else:
                out.append(Dose(None, "Stabilizer", 0, "", applyable=False,
                                note="no stabilizer chemical in catalog"))

    if water_type == "salt" and salt is not None and salt < targets["salt"] - band["salt"]:
        chem = _first(chemicals, "salt")
        if chem is not None:
            lbs = (targets["salt"] - salt) * gallons * 8.34 / 1_000_000
            _emit(out, chem, lbs, "lb")
        else:
            out.append(Dose(None, "Salt", 0, "", applyable=False,
                            note="no salt chemical in catalog"))

    return out

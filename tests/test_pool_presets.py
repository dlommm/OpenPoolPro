from app import pool_presets
from app.rates import SHAPE_MULT, gallons


def test_presets_have_valid_shapes_and_gallons():
    assert pool_presets.PRESETS
    for p in pool_presets.PRESETS:
        assert p["shape"] == "round" or p["shape"] in SHAPE_MULT, p["key"]
        assert p["kind"] in ("pool", "spa"), p["key"]
        g = gallons(p["shape"], p["length_ft"], p["width_ft"], p["avg_depth_ft"])
        assert g > 0


def test_by_key_and_grouped():
    assert pool_presets.by_key["ig_rect_16x32"]["shape"] == "rectangle"
    groups = dict(pool_presets.grouped())
    assert "Spa" in groups
    assert any(x["kind"] == "spa" for x in groups["Spa"])

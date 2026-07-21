"""Static pool/spa size presets for the visual picker.

Dimensions are the source of truth; ref_gallons is a display hint only.
Every shape must be a key in app.rates.SHAPE_MULT or the special "round".
"""

PRESETS = [
    # Above-Ground 48" wall
    {"key": "ag48_round_15", "group": "Above-Ground 48\"", "label": "15' Round",
     "shape": "round", "length_ft": 15, "width_ft": None, "avg_depth_ft": 4.0,
     "ref_gallons": 5300, "kind": "pool"},
    {"key": "ag48_round_24", "group": "Above-Ground 48\"", "label": "24' Round",
     "shape": "round", "length_ft": 24, "width_ft": None, "avg_depth_ft": 4.0,
     "ref_gallons": 13600, "kind": "pool"},
    {"key": "ag48_oval_15x30", "group": "Above-Ground 48\"", "label": "15x30 Oval",
     "shape": "oval", "length_ft": 30, "width_ft": 15, "avg_depth_ft": 4.0,
     "ref_gallons": 8900, "kind": "pool"},
    # Above-Ground 52" wall (deeper)
    {"key": "ag52_round_24", "group": "Above-Ground 52\"", "label": "24' Round",
     "shape": "round", "length_ft": 24, "width_ft": None, "avg_depth_ft": 4.33,
     "ref_gallons": 15300, "kind": "pool"},
    {"key": "ag52_oval_18x33", "group": "Above-Ground 52\"", "label": "18x33 Oval",
     "shape": "oval", "length_ft": 33, "width_ft": 18, "avg_depth_ft": 4.33,
     "ref_gallons": 11500, "kind": "pool"},
    # Inground
    {"key": "ig_rect_12x24", "group": "Inground", "label": "12x24 Rectangular",
     "shape": "rectangle", "length_ft": 24, "width_ft": 12, "avg_depth_ft": 5.0,
     "ref_gallons": 10800, "kind": "pool"},
    {"key": "ig_rect_16x32", "group": "Inground", "label": "16x32 Rectangular",
     "shape": "rectangle", "length_ft": 32, "width_ft": 16, "avg_depth_ft": 5.0,
     "ref_gallons": 19200, "kind": "pool"},
    {"key": "ig_rect_20x40", "group": "Inground", "label": "20x40 Rectangular",
     "shape": "rectangle", "length_ft": 40, "width_ft": 20, "avg_depth_ft": 5.0,
     "ref_gallons": 30000, "kind": "pool"},
    {"key": "ig_oval_16x32", "group": "Inground", "label": "16x32 Oval",
     "shape": "oval", "length_ft": 32, "width_ft": 16, "avg_depth_ft": 5.0,
     "ref_gallons": 17200, "kind": "pool"},
    {"key": "ig_grecian_17x33", "group": "Inground", "label": "17x33 Grecian",
     "shape": "grecian", "length_ft": 33, "width_ft": 17, "avg_depth_ft": 5.0,
     "ref_gallons": 19700, "kind": "pool"},
    {"key": "ig_kidney_16x30", "group": "Inground", "label": "16x30 Kidney",
     "shape": "kidney", "length_ft": 30, "width_ft": 16, "avg_depth_ft": 5.0,
     "ref_gallons": 14900, "kind": "pool"},
    # Spa
    {"key": "spa_sm", "group": "Spa", "label": "Small spa",
     "shape": "round", "length_ft": 6, "width_ft": None, "avg_depth_ft": 3.0,
     "ref_gallons": 300, "kind": "spa"},
    {"key": "spa_lg", "group": "Spa", "label": "Large spa",
     "shape": "rectangle", "length_ft": 8, "width_ft": 8, "avg_depth_ft": 3.5,
     "ref_gallons": 800, "kind": "spa"},
]

by_key = {p["key"]: p for p in PRESETS}


def grouped() -> list[tuple[str, list[dict]]]:
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for p in PRESETS:
        if p["group"] not in buckets:
            buckets[p["group"]] = []
            order.append(p["group"])
        buckets[p["group"]].append(p)
    return [(g, buckets[g]) for g in order]

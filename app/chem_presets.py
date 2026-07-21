"""Common pool-chemical presets for one-click catalog setup.

role values must be members of app.models.ROLE_VALUES.
"""

CHEM_PRESETS = [
    {"name": "Liquid chlorine", "unit": "gal", "role": "chlorine_liquid", "strength_pct": 12.5},
    {"name": "Cal-hypo shock", "unit": "lb", "role": "chlorine_granular", "strength_pct": 65.0},
    {"name": "3\" chlorine tabs", "unit": "tab", "role": "chlorine_tab", "strength_pct": 90.0},
    {"name": "Muriatic acid", "unit": "gal", "role": "ph_down", "strength_pct": 31.45},
    {"name": "Soda ash", "unit": "lb", "role": "ph_up", "strength_pct": 100.0},
    {"name": "Sodium bicarb", "unit": "lb", "role": "alkalinity_up", "strength_pct": 100.0},
    {"name": "Cyanuric acid", "unit": "lb", "role": "stabilizer", "strength_pct": 100.0},
    {"name": "Pool salt", "unit": "lb", "role": "salt", "strength_pct": 100.0},
]

by_name = {c["name"]: c for c in CHEM_PRESETS}

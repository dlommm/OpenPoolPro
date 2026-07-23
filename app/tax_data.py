"""Bundled US state sales-tax base rates (seed defaults, as of 2026-07).

Statewide base rates only — NOT county/city/district. Owner overrides any
state in Settings where a local combined rate matters. Non-overridden rows
refresh to these values on each app boot, so a shipped update propagates.
"""
# state -> statewide base sales-tax percent
STATE_SALES_TAX = {
    "AL": 4.0, "AK": 0.0, "AZ": 5.6, "AR": 6.5, "CA": 7.25, "CO": 2.9,
    "CT": 6.35, "DE": 0.0, "FL": 6.0, "GA": 4.0, "HI": 4.0, "ID": 6.0,
    "IL": 6.25, "IN": 7.0, "IA": 6.0, "KS": 6.5, "KY": 6.0, "LA": 5.0,
    "ME": 5.5, "MD": 6.0, "MA": 6.25, "MI": 6.0, "MN": 6.875, "MS": 7.0,
    "MO": 4.225, "MT": 0.0, "NE": 5.5, "NV": 6.85, "NH": 0.0, "NJ": 6.625,
    "NM": 4.875, "NY": 4.0, "NC": 4.75, "ND": 5.0, "OH": 5.75, "OK": 4.5,
    "OR": 0.0, "PA": 6.0, "RI": 7.0, "SC": 6.0, "SD": 4.2, "TN": 7.0,
    "TX": 6.25, "UT": 6.1, "VT": 6.0, "VA": 5.3, "WA": 6.5, "WV": 6.0,
    "WI": 5.0, "WY": 4.0, "DC": 6.0,
}

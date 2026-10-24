from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

CHEMICALS = [
    ("Liquid chlorine", "gal"), ("Muriatic acid", "gal"), ("Chlorine tabs 3in", "tab"),
    ("Shock (cal-hypo)", "lb"), ("Salt", "lb"), ("Soda ash", "lb"),
    ("Baking soda", "lb"), ("Stabilizer (CYA)", "lb"),
]
TASKS = ["Skim surface", "Brush walls", "Vacuum", "Empty skimmer baskets",
         "Empty pump trap", "Backwash/clean filter", "Test water", "Add chemicals"]
TIERS = [(15000, 4500, "Small (to 15k gal)"), (25000, 5500, "Medium (to 25k gal)"),
         (None, 7000, "Large")]
SPA_TIERS = [(600, 2000, "Spa small (to 600 gal)"), (None, 3000, "Spa large")]
# Common pool/spa sizes seeded into the managed size library.
# (kind, label, shape, length_ft, width_ft, avg_depth_ft, ref_gallons)
# ref_gallons is the authoritative volume shown in the picker/rate quick-add;
# spas carry an explicit value because seats/shape make the pool formula wrong.
SIZE_PRESETS = [
    # Above-ground rounds
    ("pool", "12' Round", "round", 12, None, 4.0, 3000),
    ("pool", "15' Round", "round", 15, None, 4.0, 5300),
    ("pool", "18' Round", "round", 18, None, 4.0, 7600),
    ("pool", "21' Round", "round", 21, None, 4.0, 10400),
    ("pool", "24' Round", "round", 24, None, 4.0, 13600),
    ("pool", "27' Round", "round", 27, None, 4.0, 17200),
    ("pool", "28' Round", "round", 28, None, 4.0, 18500),
    ("pool", "30' Round", "round", 30, None, 4.0, 21000),
    ("pool", "33' Round", "round", 33, None, 4.0, 25000),
    # Above-ground ovals
    ("pool", "12x24 Oval", "oval", 24, 12, 4.0, 5900),
    ("pool", "15x24 Oval", "oval", 24, 15, 4.0, 7500),
    ("pool", "15x30 Oval", "oval", 30, 15, 4.0, 8900),
    ("pool", "16x32 Oval", "oval", 32, 16, 4.0, 10500),
    ("pool", "18x33 Oval", "oval", 33, 18, 4.0, 10600),
    ("pool", "18x40 Oval", "oval", 40, 18, 4.0, 14000),
    # Inground rectangles
    ("pool", "10x20 Rectangle", "rectangle", 20, 10, 4.5, 6750),
    ("pool", "12x24 Rectangle", "rectangle", 24, 12, 5.0, 10800),
    ("pool", "14x28 Rectangle", "rectangle", 28, 14, 5.0, 14700),
    ("pool", "15x30 Rectangle", "rectangle", 30, 15, 5.0, 16900),
    ("pool", "16x32 Rectangle", "rectangle", 32, 16, 5.0, 19200),
    ("pool", "16x36 Rectangle", "rectangle", 36, 16, 5.0, 21600),
    ("pool", "18x36 Rectangle", "rectangle", 36, 18, 5.0, 24300),
    ("pool", "20x40 Rectangle", "rectangle", 40, 20, 5.0, 30000),
    ("pool", "20x44 Rectangle", "rectangle", 44, 20, 5.0, 33000),
    ("pool", "25x45 Rectangle", "rectangle", 45, 25, 5.0, 42000),
    ("pool", "25x50 Rectangle", "rectangle", 50, 25, 5.0, 47000),
    # Inground ovals
    ("pool", "16x32 Oval (IG)", "oval", 32, 16, 5.0, 17200),
    ("pool", "18x36 Oval (IG)", "oval", 36, 18, 5.0, 21700),
    ("pool", "20x40 Oval (IG)", "oval", 40, 20, 5.0, 26800),
    # Grecian
    ("pool", "16x32 Grecian", "grecian", 32, 16, 5.0, 18400),
    ("pool", "17x33 Grecian", "grecian", 33, 17, 5.0, 19700),
    ("pool", "17x37 Grecian", "grecian", 37, 17, 5.0, 22200),
    ("pool", "20x36 Grecian", "grecian", 36, 20, 5.0, 24300),
    ("pool", "20x40 Grecian", "grecian", 40, 20, 5.0, 28000),
    ("pool", "20x44 Grecian", "grecian", 44, 20, 5.0, 30300),
    # Kidney
    ("pool", "16x30 Kidney", "kidney", 30, 16, 5.0, 14900),
    ("pool", "16x34 Kidney", "kidney", 34, 16, 5.0, 16500),
    ("pool", "18x36 Kidney", "kidney", 36, 18, 5.0, 19400),
    ("pool", "20x38 Kidney", "kidney", 38, 20, 5.0, 20200),
    ("pool", "20x40 Kidney", "kidney", 40, 20, 5.0, 24000),
    # Spas / hot tubs (explicit gallons — pool formula does not fit)
    ("spa", "2-person spa", "rectangle", 5.5, 5.5, 2.5, 175),
    ("spa", "3-person spa", "rectangle", 6, 5.5, 2.75, 225),
    ("spa", "4-person spa", "rectangle", 6.5, 6.5, 3.0, 300),
    ("spa", "5-person spa", "rectangle", 7, 7, 3.0, 350),
    ("spa", "6-person spa", "rectangle", 7, 7, 3.5, 425),
    ("spa", "7-person spa", "rectangle", 7.5, 7.5, 3.5, 500),
    ("spa", "8-person spa", "rectangle", 8, 8, 3.5, 575),
    ("spa", "Round spa (small)", "round", 6, None, 3.0, 300),
    ("spa", "Round spa (large)", "round", 7.5, None, 3.5, 450),
    ("spa", "In-ground spa", "rectangle", 8, 8, 3.5, 650),
    ("spa", "Swim spa (small)", "rectangle", 13, 7, 4.0, 1450),
    ("spa", "Swim spa (large)", "rectangle", 19, 8, 4.5, 2500),
]
ROLE_SEED = {
    "Liquid chlorine": ("chlorine_liquid", 12.5),
    "Shock (cal-hypo)": ("chlorine_granular", 65.0),
    "Chlorine tabs 3in": ("chlorine_tab", 90.0),
    "Muriatic acid": ("ph_down", 31.45),
    "Soda ash": ("ph_up", 100.0),
    "Baking soda": ("alkalinity_up", 100.0),
    "Stabilizer (CYA)": ("stabilizer", 100.0),
    "Salt": ("salt", 100.0),
}


def seed_tax_rates(session: Session) -> None:
    """Insert missing state rows; refresh non-overridden rows to bundled seed;
    leave owner-overridden rows untouched."""
    from app.tax import parse_percent
    from app.tax_data import STATE_SALES_TAX
    for state, pct in STATE_SALES_TAX.items():
        seed_ppm = parse_percent(str(pct))
        row = session.get(models.TaxRate, state)
        if row is None:
            session.add(models.TaxRate(state=state, rate_ppm=seed_ppm, overridden=False))
        elif not row.overridden:
            row.rate_ppm = seed_ppm


def run(engine) -> None:
    with Session(engine) as s:
        if not s.scalars(select(models.Chemical)).first():
            for name, unit in CHEMICALS:
                s.add(models.Chemical(name=name, unit=unit, cost_per_unit_cents=0))
        if not s.scalars(select(models.TaskTemplate)).first():
            for i, label in enumerate(TASKS):
                s.add(models.TaskTemplate(label=label, sort_order=i))
        if not s.scalars(select(models.RateTier)).first():
            for max_g, cents, label in TIERS:
                s.add(models.RateTier(max_gallons=max_g, rate_cents=cents, label=label, kind="pool"))
            for max_g, cents, label in SPA_TIERS:
                s.add(models.RateTier(max_gallons=max_g, rate_cents=cents, label=label, kind="spa"))
        # Backfill for installs upgraded from before spa tiers existed: pool tiers
        # already present but spa tiers were never seeded (spa would price at $0).
        if not s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).first():
            for max_g, cents, label in SPA_TIERS:
                s.add(models.RateTier(max_gallons=max_g, rate_cents=cents, label=label, kind="spa"))
        if not s.scalars(select(models.SizePreset)).first():
            for i, (kind, label, shape, length, width, depth, ref_g) in enumerate(SIZE_PRESETS):
                s.add(models.SizePreset(kind=kind, label=label, shape=shape, length_ft=length,
                                        width_ft=width, avg_depth_ft=depth, ref_gallons=ref_g,
                                        sort_order=i))
        # No owner is created here: the first-run setup (app/setup_flow.py)
        # makes one. An install upgraded from the APP_PASSWORD era keeps its
        # "owner" account and is asked once to finish the profile.
        if not models.get_setting(s, "setup_status", ""):
            admin = s.scalars(select(models.User).where(
                models.User.role == "admin", models.User.active,
                models.User.password_hash.is_not(None)).order_by(models.User.id)).first()
            if admin is not None:
                models.set_setting(s, "setup_status",
                                   "legacy" if admin.username == "owner" else "done")
        for chem in s.scalars(select(models.Chemical)).all():
            if chem.role == "other" and chem.name in ROLE_SEED:
                chem.role, chem.strength_pct = ROLE_SEED[chem.name]
        seed_tax_rates(s)
        s.commit()

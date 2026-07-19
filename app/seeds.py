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
                s.add(models.RateTier(max_gallons=max_g, rate_cents=cents, label=label))
        from app import config
        from app.auth_core import hash_password, verify_password

        owner = s.scalars(select(models.User).where(models.User.username == "owner")).first()
        if owner is None:
            # Recovery path: APP_PASSWORD env is authoritative for the owner account.
            # Any future in-app owner password change will be overwritten at next boot
            # unless .env is updated in lockstep.
            legacy = s.scalars(select(models.User).where(models.User.username.is_(None))).first()
            if legacy is not None:
                legacy.username, legacy.role, legacy.active = "owner", "admin", True
                legacy.password_hash = hash_password(config.APP_PASSWORD)
            else:
                s.add(models.User(name="Owner", username="owner", role="admin", active=True,
                                  password_hash=hash_password(config.APP_PASSWORD)))
        elif not verify_password(config.APP_PASSWORD, owner.password_hash):
            owner.password_hash = hash_password(config.APP_PASSWORD)  # env = recovery path
        for chem in s.scalars(select(models.Chemical)).all():
            if chem.role == "other" and chem.name in ROLE_SEED:
                chem.role, chem.strength_pct = ROLE_SEED[chem.name]
        s.commit()

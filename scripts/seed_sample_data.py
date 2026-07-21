"""Populate the database with realistic demo data for LOCAL preview/testing.

Idempotent: does nothing if it has already run (guarded by a `sample_seeded`
setting) or if real customers already exist. Never run against production data.

Run inside the app container after it boots:
    docker compose exec app python scripts/seed_sample_data.py
or on the host with the app's data dir:
    DATA_DIR=./data .venv/bin/python scripts/seed_sample_data.py
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import make_engine
from app.rates import effective_rate_cents, gallons


def run() -> None:
    engine = make_engine()
    with Session(engine) as s:
        if models.get_setting(s, "sample_seeded", "") == "1":
            print("sample data already seeded — skipping")
            return

        # --- Company profile + billing settings ---
        for key, value in [
            ("business_name", "Coastline Pools"),
            ("company_address", "142 Marina Way, Naples, FL 34102"),
            ("company_phone", "(239) 555-0142"),
            ("company_email", "service@coastlinepools.example"),
            ("company_license", "CPC-1458792"),
            ("chem_markup_percent", "15"),
        ]:
            models.set_setting(s, key, value)

        # --- Realistic per-unit costs on the seeded chemical catalog (cents) ---
        chem_costs = {
            "Liquid chlorine": 1250, "Muriatic acid": 899, "Chlorine tabs 3in": 1899,
            "Shock (cal-hypo)": 549, "Salt": 799, "Soda ash": 1299,
            "Baking soda": 699, "Stabilizer (CYA)": 2499,
        }
        for c in s.scalars(select(models.Chemical)).all():
            if c.cost_per_unit_cents == 0 and c.name in chem_costs:
                c.cost_per_unit_cents = chem_costs[c.name]

        # --- Job templates with checklists ---
        if not s.scalars(select(models.JobTemplate)).first():
            green = models.JobTemplate(title="Green-to-clean recovery",
                                       description="Algae bloom recovery",
                                       job_type="service", labor_cents=15000)
            green.tasks = [models.JobTemplateTask(label=x, position=i) for i, x in enumerate(
                ["Test water", "Brush walls & floor", "Add shock", "Add clarifier",
                 "Run filter 24h", "Vacuum to waste"])]
            filt = models.JobTemplate(title="Filter clean & inspect",
                                      description="Cartridge clean",
                                      job_type="repair", labor_cents=9500)
            filt.tasks = [models.JobTemplateTask(label=x, position=i) for i, x in enumerate(
                ["Shut off pump", "Remove cartridges", "Rinse & degrease",
                 "Inspect O-rings", "Reassemble & prime"])]
            s.add_all([green, filt])

        # --- Demo customers / properties / pools ---
        # (name, phone, email, address, frequency, poolspec, spaspec-or-None)
        # spec = (label, shape, length_ft, width_ft, avg_depth_ft, water_type, surface, kind)
        demo = [
            ("Marcus & Lena Alvarez", "(239) 555-0111", "marcus.alvarez@example.com",
             "88 Gulfshore Blvd", "weekly",
             ("Main pool", "rectangle", 32, 16, 5.0, "chlorine", "pebble", "pool"),
             ("Spa", "rectangle", 8, 8, 3.5, "chlorine", "tile", "spa")),
            ("Priya Nair", "(239) 555-0122", "priya.nair@example.com",
             "17 Seagate Dr", "biweekly",
             ("Backyard", "oval", 30, 15, 4.5, "salt", "plaster", "pool"), None),
            ("The Whitfield Estate", "(239) 555-0133", "estate@whitfield.example",
             "5 Port Royal Ln", "weekly",
             ("Lap pool", "rectangle", 40, 20, 5.5, "salt", "tile", "pool"), None),
            ("Dana Cole", "(239) 555-0144", "dana.cole@example.com",
             "230 Banyan St", "weekly",
             ("Above-ground", "round", 24, None, 4.33, "chlorine", "vinyl", "pool"), None),
        ]
        if not s.scalars(select(models.Customer)).first():
            for name, phone, email, addr, freq, poolspec, spaspec in demo:
                cust = models.Customer(name=name, phone=phone, email=email,
                                       active=True, notify_email=True)
                prop = models.Property(customer=cust, label="Home", address=addr,
                                       frequency=freq, weekday=1, anchor_date=date.today())
                s.add(prop)
                for spec in (poolspec, spaspec):
                    if not spec:
                        continue
                    label, shape, length_ft, width_ft, depth, water, surface, kind = spec
                    s.add(models.Pool(
                        property=prop, label=label, shape=shape, length_ft=length_ft,
                        width_ft=width_ft, avg_depth_ft=depth,
                        gallons=gallons(shape, length_ft, width_ft, depth),
                        water_type=water, surface=surface, kind=kind, active=True))
            s.flush()

            first_prop = s.scalars(select(models.Property).order_by(models.Property.id)).first()
            if first_prop is not None:
                rate = effective_rate_cents(s, first_prop)
                for wk in range(1, 5):
                    d = date.today() - timedelta(weeks=wk)
                    s.add(models.Visit(
                        property_id=first_prop.id, date=d, status="done", resolved=True,
                        rate_cents=rate, completed_at=models.utcnow(),
                        fc=3.4, ph=7.6, ta=90, cya=45, salt=0,
                        tasks_done=["Skim surface", "Brush walls", "Test water"]))
                s.add(models.Job(
                    property_id=first_prop.id, title="Replace pump seal",
                    description="Slight drip at the seal plate — schedule this week.",
                    status="open", priority="normal", job_type="repair", labor_cents=12000))

        models.set_setting(s, "sample_seeded", "1")
        s.commit()
        print("sample data seeded")


if __name__ == "__main__":
    run()

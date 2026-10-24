# ruff: noqa: E501 -- the customer table below reads best one row per line
"""A fuller synthetic company for LOCAL preview and visual checks.

The V2 design prototype was drawn against an invented 22-customer company
(three people on routes, the owner among them). This seeds the same shape so
every redesigned screen has real variety to render: today's routes for three
people with some stops done, a season of readings, open jobs, quotes,
invoices that are paid, part-paid and overdue, equipment with warranties
running out, and a low-stock chemical.

Every name, address and amount is made up. Refuses to touch a database that
already has customers. Run after the app has booted once (so the owner login
and catalog exist):

    DATA_DIR=./data .venv/bin/python scripts/seed_demo.py
"""
import random
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import addresses, models
from app.auth_core import hash_password
from app.db import make_engine
from app.rates import effective_rate_cents, gallons

# (name, contact email, city, street, zip, kind, water, tech, tags note)
CUSTOMERS = [
    ("The Whitfield Estate", "helen.whitfield", "Satellite Beach", "5 Port Royal Ln", "32937", "pool+spa", "chlorine", "jordan", "Spa heater job open"),
    ("Priya Nair", "priya.nair", "Melbourne", "17 Seagate Dr", "32901", "pool", "salt", "jordan", ""),
    ("Dana Cole", "dana.cole", "Merritt Island", "230 Banyan St", "32952", "pool", "chlorine", "jordan", ""),
    ("Marcus & Lena Alvarez", "marcus.alvarez", "Cocoa Beach", "88 Gulfshore Blvd", "32931", "pool", "chlorine", "jordan", "Side gate sticks — lift the latch"),
    ("Tom Brennan", "tom.brennan", "Cocoa", "41 Riverside Dr", "32922", "pool", "salt", "jordan", "Dog in the yard — friendly"),
    ("Grace Huang", "grace.huang", "Indialantic", "1200 Atlantic Ave", "32903", "pool", "chlorine", "jordan", ""),
    ("Pete Ostrowski", "pete.ostrowski", "Satellite Beach", "9 Pelican Ct", "32937", "pool", "chlorine", "jordan", ""),
    ("Ruth Delgado", "ruth.delgado", "Rockledge", "612 Barton Blvd", "32955", "pool", "chlorine", "sam", ""),
    ("Laurel Pines HOA", "board", "Viera", "3100 Laurel Pines Way", "32940", "pool+spa", "chlorine", None, "New account"),
    ("Kenji Watanabe", "kenji.watanabe", "Melbourne Beach", "505 Ocean Ave", "32951", "pool", "chlorine", "sam", ""),
    ("Maria Santos", "maria.santos", "Palm Bay", "1840 Malabar Rd", "32907", "pool", "chlorine", "sam", ""),
    ("Ben & Ava Carter", "ava.carter", "West Melbourne", "77 Hollywood Blvd", "32904", "pool", "chlorine", "sam", ""),
    ("Coral Shores Condos", "manager", "Cocoa Beach", "2200 S Atlantic Ave", "32931", "pool", "chlorine", "sam", "Commercial"),
    ("Diane Foster", "diane.foster", "Titusville", "15 Harbor Pt", "32780", "pool", "chlorine", "jordan", "Travelling"),
    ("Luis Rivera", "luis.rivera", "Melbourne", "930 Hibiscus Blvd", "32901", "pool", "chlorine", "sam", ""),
    ("Anna Lindqvist", "anna.lindqvist", "Melbourne Beach", "12 Sea Grape Ct", "32951", "pool", "chlorine", "sam", ""),
    ("Raj Patel", "raj.patel", "West Melbourne", "4021 Minton Rd", "32904", "pool", "salt", "sam", ""),
    ("Sofia Gomez", "sofia.gomez", "Palm Bay", "260 Emerson Dr", "32907", "pool", "chlorine", "sam", ""),
    ("Bayside Racquet Club", "facilities", "Viera", "7400 Murrell Rd", "32940", "pool", "chlorine", "owner", "Check in at the front desk"),
    ("Hannah Brooks", "hannah.brooks", "Rockledge", "88 Fiske Blvd", "32955", "pool", "chlorine", "owner", ""),
    ("Oliver Grant", "oliver.grant", "Cocoa", "300 Clearlake Rd", "32922", "pool+spa", "salt", "owner", ""),
    ("Nadia Petrova", "nadia.petrova", "Indian Harbour Beach", "45 Yacht Club Blvd", "32937", "pool", "chlorine", "sam", ""),
]


def run() -> None:
    rnd = random.Random(7)
    engine = make_engine()
    today = date.today()
    with Session(engine) as s:
        if s.scalars(select(models.Customer)).first() is not None:
            print("existing customers found — refusing to change business data")
            return
        for key, value in [
            ("business_name", "Coastline Pool Co."),
            ("company_address", "142 Marina Way, Melbourne, FL 32901"),
            ("company_phone", "(321) 555-0142"),
            ("company_email", "service@coastline.example"),
            ("company_license", "CPC-1458792"),
            ("chem_markup_percent", "15"),
        ]:
            models.set_setting(s, key, value)

        owner = s.scalars(select(models.User).where(models.User.role == "admin")
                          .order_by(models.User.id)).first()
        if owner is not None and owner.name in ("", "Owner", "owner"):
            owner.name = "Alex Morgan"
        techs = {}
        for key, name, user in (("jordan", "Jordan Reyes", "jordan"), ("sam", "Sam Okafor", "sam")):
            t = models.User(name=name, role="tech", username=user,
                            password_hash=hash_password("demo-" + user), active=True)
            s.add(t)
            techs[key] = t
        techs["owner"] = owner
        s.flush()

        costs = {"Liquid chlorine": 1250, "Muriatic acid": 899, "Chlorine tabs 3in": 1899,
                 "Shock (cal-hypo)": 549, "Salt": 799, "Soda ash": 1299,
                 "Baking soda": 699, "Stabilizer (CYA)": 2499}
        for c in s.scalars(select(models.Chemical)).all():
            if c.name in costs:
                c.cost_per_unit_cents = costs[c.name]
            if c.name == "Muriatic acid" and hasattr(c, "stock_qty"):
                c.stock_qty, c.reorder_point = 2.0, 6.0

        weekday = today.weekday()
        props = []
        for n, (name, mail, city, street, zip_, kind, water, tech, note) in enumerate(CUSTOMERS):
            domain = "example.com" if "." in mail else name.split()[0].lower() + ".example.com"
            cust = models.Customer(name=name, phone=f"(321) 555-{100 + n:04d}",
                                   email=f"{mail}@{domain}", active=True, notify_email=True)
            on_today = tech is not None and name != "Diane Foster"
            prop = models.Property(
                customer=cust, label="Home", street=street, city=city, state="FL", zip=zip_,
                address=addresses.compose_address(street, city, "FL", zip_),
                frequency="weekly", weekday=weekday if on_today else (weekday + 2) % 7,
                anchor_date=today - timedelta(weeks=8), route_order=n,
                assigned_tech_id=techs[tech].id if tech and techs.get(tech) else None)
            if note:
                prop.notes = note
            if "Dog" in note:
                cust.tags = ["Dog"]
            if "gate" in note:
                prop.gate_code = "4417"
            s.add(prop)
            length, width, depth = rnd.choice([(32, 16, 5.0), (30, 15, 4.5), (36, 18, 5.5), (28, 14, 4.5)])
            if "Condos" in name or "Club" in name or "HOA" in name:
                length, width, depth = 60, 30, 5.0
            s.add(models.Pool(property=prop, label="Main pool", shape="rectangle",
                              length_ft=length, width_ft=width, avg_depth_ft=depth,
                              gallons=gallons("rectangle", length, width, depth),
                              water_type=water, surface="plaster", kind="pool", active=True))
            if kind == "pool+spa":
                s.add(models.Pool(property=prop, label="Spa", shape="rectangle", length_ft=8,
                                  width_ft=8, avg_depth_ft=3.5, gallons=gallons("rectangle", 8, 8, 3.5),
                                  water_type=water, surface="tile", kind="spa", active=True))
            props.append((prop, tech, water))
        s.flush()

        # A season of readings, then today's progress: some stops done.
        done_today = {"Priya Nair", "The Whitfield Estate", "Ruth Delgado", "Kenji Watanabe",
                      "Maria Santos"}
        for prop, _tech, water in props:
            rate = effective_rate_cents(s, prop)
            for wk in range(1, 9):
                d = today - timedelta(weeks=wk)
                fc = round(rnd.uniform(1.0, 5.5), 1)
                if prop.customer.name == "Priya Nair":
                    fc = round(rnd.uniform(0.6, 1.4), 1)
                s.add(models.Visit(
                    property_id=prop.id, date=d, status="done", resolved=True, rate_cents=rate,
                    completed_at=models.utcnow() - timedelta(weeks=wk),
                    fc=fc, ph=round(rnd.uniform(7.2, 7.9), 1), ta=rnd.randint(70, 120),
                    cya=rnd.randint(30, 70), salt=rnd.randint(2900, 3600) if water == "salt" else 0,
                    tasks_done=["Skim surface", "Brush walls", "Test water"]))
            if prop.customer.name in done_today and prop.weekday == today.weekday():
                s.add(models.Visit(
                    property_id=prop.id, date=today, status="done", resolved=True, rate_cents=rate,
                    completed_at=models.utcnow() - timedelta(hours=rnd.randint(1, 3)),
                    fc=3.2, ph=7.5, ta=95, cya=45, salt=3200 if water == "salt" else 0,
                    tasks_done=["Skim surface", "Brush walls", "Test water"]))

        by_name = {p.customer.name: p for p, _, _ in props}
        for cname, title, status, prio, jtype in [
            ("The Whitfield Estate", "Spa heater igniter", "open", "high", "repair"),
            ("Ruth Delgado", "Leak check", "scheduled", "normal", "service"),
            ("Grace Huang", "Pump making noise", "open", "normal", "repair"),
            ("Coral Shores Condos", "Replace skimmer lid", "open", "low", "repair"),
            ("Oliver Grant", "Green-to-clean recovery", "open", "high", "service"),
            ("Tom Brennan", "Salt cell inspection", "done", "normal", "service"),
        ]:
            s.add(models.Job(property_id=by_name[cname].id, title=title, status=status,
                             priority=prio, job_type=jtype, labor_cents=12000,
                             scheduled_date=today + timedelta(days=2) if status == "scheduled" else None))

        for cname, title, status, lines in [
            ("Laurel Pines HOA", "Season service agreement", "sent", [("Weekly service, 40 weeks", 1040000)]),
            ("Anna Lindqvist", "Variable-speed pump upgrade", "draft", [("Pump", 129900), ("Install labor", 35000)]),
            ("Raj Patel", "Salt cell replacement", "accepted", [("Salt cell", 74900), ("Labor", 12000)]),
        ]:
            q = models.Quote(customer_id=by_name[cname].customer_id, property_id=by_name[cname].id,
                             title=title, status=status)
            q.lines = [models.QuoteLine(description=d, amount_cents=a, sort_order=i)
                       for i, (d, a) in enumerate(lines)]
            s.add(q)

        # Invoices for last month: most paid, some part-paid, two overdue.
        last = (today.replace(day=1) - timedelta(days=1))
        before = (last.replace(day=1) - timedelta(days=1))
        num = 1001
        for period, due_offset in ((before, 30), (last, 15)):
            for i, (prop, _, _) in enumerate(props):
                total = effective_rate_cents(s, prop) * 4
                inv = models.Invoice(
                    number=f"INV-{num}", customer_id=prop.customer_id,
                    period_year=period.year, period_month=period.month,
                    total_cents=total, subtotal_cents=total, status="sent",
                    due_date=period + timedelta(days=due_offset), sent_at=models.utcnow(),
                    pdf_filename=f"INV-{num}.pdf")
                num += 1
                s.add(inv)
                s.flush()
                paid = (period is before and i not in (12, 14)) or (period is last and i % 3 == 0)
                if paid:
                    s.add(models.Payment(invoice_id=inv.id, amount_cents=total,
                                         received_on=period + timedelta(days=rnd.randint(8, 20)),
                                         method=rnd.choice(["check", "card", "ach"])))
                elif period is last and i % 5 == 1:
                    s.add(models.Payment(invoice_id=inv.id, amount_cents=total // 2,
                                         received_on=today - timedelta(days=3), method="check"))

        # Equipment with warranties running out.
        for cname, kind, brand, model, days in [
            ("Grace Huang", "pump", "Pentair", "SuperFlo VS", 52),
            ("Raj Patel", "salt cell", "Hayward", "TurboCell T-15", 38),
            ("The Whitfield Estate", "heater", "Raypak", "Avia 266k", 400),
        ]:
            pool = s.scalars(select(models.Pool).where(models.Pool.property_id == by_name[cname].id)).first()
            s.add(models.Equipment(pool_id=pool.id, kind=kind, brand=brand, model=model,
                                   install_date=today - timedelta(days=700),
                                   warranty_end=today + timedelta(days=days)))

        models.set_setting(s, "sample_seeded", "1")
        s.commit()
        print(f"demo company seeded: {len(CUSTOMERS)} customers, owner + 2 technicians")


if __name__ == "__main__":
    run()

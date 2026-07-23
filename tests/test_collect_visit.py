"""Task 10: pay-at-job collect price on the visit completion form.

The completer (INCLUDING a tech, who normally sees no money) must see the
final customer PRICE to collect on this visit -- service + chemicals + tax.
This is the one sanctioned money-gate exception: PRICE only, never cost,
never markup.
"""
from sqlalchemy.orm import Session

from app import models, seeds


def _seed_taxed_property(client, engine):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    client.post("/customers/1/properties/new", data={
        "label": "Home", "address": "", "state": "NJ", "gate_code": "",
        "dog_note": "", "notes": "", "frequency": "weekly", "weekday": "0",
        "anchor_date": "", "rate_override_dollars": ""})
    client.post("/properties/1/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""})
    with Session(engine) as s:
        models.set_setting(s, "sales_tax_enabled", "1")
        seeds.seed_tax_rates(s)
        s.commit()


def test_tech_sees_collect_price_not_cost(client, tech_client, engine):
    _seed_taxed_property(client, engine)
    t = tech_client(name="fieldtech")
    html = t.get("/visit/1/2026-07-13").text
    assert "Collect" in html
    assert "$55.00" in html            # service price (Medium tier), visible to tech
    assert "cost" not in html.lower()  # chemical cost/markup never exposed

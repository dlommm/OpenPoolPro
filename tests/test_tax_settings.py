from sqlalchemy.orm import Session

from app import models, seeds, tax


def test_save_tax_general(client, engine):
    client.post("/settings/tax", data={"sales_tax_enabled": "on",
                                        "sales_tax_default_pct": "6.5",
                                        "tax_labor": "on", "tax_chems": "on", "tax_weekly": ""})
    with Session(engine) as s:
        assert models.get_setting(s, "sales_tax_enabled", "0") == "1"
        assert models.get_setting(s, "sales_tax_default_ppm", "0") == "65000"
        assert models.get_setting(s, "tax_weekly", "1") == "0"


def test_edit_state_rate_sets_override(client, engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s); s.commit()
    client.post("/settings/tax/state", data={"state": "NJ", "pct": "7.0"})
    with Session(engine) as s:
        nj = s.get(models.TaxRate, "NJ")
        assert nj.rate_ppm == 70000 and nj.overridden is True


def test_reset_state_rate_clears_override(client, engine):
    with Session(engine) as s:
        seeds.seed_tax_rates(s); s.commit()
    client.post("/settings/tax/state", data={"state": "NJ", "pct": "9"})
    client.post("/settings/tax/state/NJ/reset")
    with Session(engine) as s:
        nj = s.get(models.TaxRate, "NJ")
        assert nj.rate_ppm == 66250 and nj.overridden is False


def test_bare_tech_cannot_edit_tax(tech_client):
    t = tech_client(name="bare")
    assert t.post("/settings/tax", data={}, follow_redirects=False).status_code == 403

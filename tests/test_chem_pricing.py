from app import qbo


def test_per_item_markup_helper():
    class C:
        markup_percent = 50.0

    class VC:
        total_cost_cents = 1000
        chemical = C()

    vc = VC()
    assert qbo.billable_for_vc(vc, 0.0) == 1500      # per-item 50% wins over global 0
    C.markup_percent = None
    assert qbo.billable_for_vc(vc, 20.0) == 1200     # blank per-item -> global 20%
    vc.chemical = None
    assert qbo.billable_for_vc(vc, 20.0) == 1200     # missing chemical -> global


def test_save_chemicals_persists_markup(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import models
    r = client.post("/settings/chemicals", data={
        "chem_id": [""], "name": ["Special sauce"], "unit": ["gal"],
        "cost_dollars": ["10"], "markup": ["40"], "role": ["other"],
        "strength_pct": [""]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.scalars(select(models.Chemical)
                      .where(models.Chemical.name == "Special sauce")).one()
        assert c.markup_percent == 40.0


def test_settings_shows_customer_cost_and_markup(client):
    html = client.get("/settings").text
    assert "Your cost" in html and "Customer" in html and "Markup" in html
    assert "opChemCust" in html  # live customer-cost script present


def test_income_analytics_has_profit(client):
    html = client.get("/analytics?tab=income").text
    assert "Profit" in html

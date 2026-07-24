from types import SimpleNamespace

from app import contract_tokens as ct


def _fixtures():
    contract = SimpleNamespace(weekly_cents=8500, spa_cents=6000, open_close_cents=40000,
                               green_clean_cents=12000, storm_cents=9000,
                               service_call_cents=15000, weeks=32, includes_spa=True)
    pool = SimpleNamespace(label="Main", gallons=18432, water_type="salt",
                           surface="plaster", length_ft=16, width_ft=32, avg_depth_ft=5)
    customer = SimpleNamespace(name="Marcus Alvarez")
    prop = SimpleNamespace(label="Home", address="88 Gulfshore Blvd, Cocoa Beach, FL 32931",
                           gate_code="4417", city="Cocoa Beach", state="FL", zip="32931")
    company = {"business_name": "Coastline Pools", "company_address": "142 Marina Way",
               "company_phone": "(239) 555-0142", "company_email": "svc@x.example",
               "company_license": "CPC-1458792"}
    return contract, pool, prop, customer, company


def test_resolve_formats_money_and_fields():
    contract, pool, prop, customer, company = _fixtures()
    ctx = ct.resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm=66250)
    assert ctx["company_name"] == "Coastline Pools"
    assert ctx["customer_name"] == "Marcus Alvarez"
    assert ctx["pool_label"] == "Main"
    assert ctx["pool_gallons"] == "18,432"
    assert ctx["pool_water_type"] == "salt"
    assert ctx["weekly_price"] == "$85.00"
    assert ctx["spa_price"] == "$60.00"
    assert ctx["season_weeks"] == "32"
    assert ctx["season_total"] == "$2,720.00"          # 32 * 8500 cents
    assert ctx["sales_tax"] == "$206.70"               # tax_cents(272000+40000, 66250)


def test_apply_substitutes_known_leaves_unknown_blank_and_whitespace_tolerant():
    ctx = {"company_name": "Coastline Pools", "weekly_price": "$85.00"}
    out = ct.apply_tokens("{{company_name}} charges {{ weekly_price }}/week; {{bogus}}!", ctx)
    assert out == "Coastline Pools charges $85.00/week; !"


def test_apply_tolerates_none():
    assert ct.apply_tokens(None, {}) == ""


def test_every_token_resolves_to_a_string():
    contract, pool, prop, customer, company = _fixtures()
    ctx = ct.resolve_tokens(contract, pool, prop, customer, company, tax_rate_ppm=0)
    for token, _label, _group in ct.TOKENS:
        assert token in ctx and isinstance(ctx[token], str), token

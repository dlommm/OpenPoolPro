from tests.test_analytics import RANGE, _fix

TABS = ["income", "customers", "crew", "types", "chemicals", "volume", "rates"]


def _get(client, tab, start=None, end=None):
    params = {"tab": tab}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return client.get("/analytics", params=params)


def test_income_tab_shows_week_labor(client, engine):
    _fix(engine)
    r = _get(client, "income", RANGE[0].isoformat(), RANGE[1].isoformat())
    assert r.status_code == 200
    assert "$120.00" in r.text


def test_customers_tab_orders_desc_by_total(client, engine):
    _fix(engine)
    r = _get(client, "customers", RANGE[0].isoformat(), RANGE[1].isoformat())
    assert r.status_code == 200
    assert r.text.index("Beta") < r.text.index("Alpha")


def test_crew_tab_unattributed_and_dash_for_no_minutes(client, engine):
    _fix(engine)
    r = _get(client, "crew", RANGE[0].isoformat(), RANGE[1].isoformat())
    assert r.status_code == 200
    assert "Unattributed" in r.text
    assert "—" in r.text  # em-dash placeholder for None per-hour


def test_all_tabs_admin_200(client, engine):
    _fix(engine)
    for tab in TABS:
        r = _get(client, tab, RANGE[0].isoformat(), RANGE[1].isoformat())
        assert r.status_code == 200, f"{tab} -> {r.status_code}"


def test_unknown_tab_defaults_to_income(client, engine):
    _fix(engine)
    r = _get(client, "bogus", RANGE[0].isoformat(), RANGE[1].isoformat())
    assert r.status_code == 200
    assert "$120.00" in r.text


def test_tech_forbidden(client, engine, tech_client):
    _fix(engine)
    t = tech_client(name="notadmin")
    assert t.get("/analytics").status_code == 403


def test_reversed_range_swaps(client, engine):
    _fix(engine)
    r = _get(client, "income", RANGE[1].isoformat(), RANGE[0].isoformat())
    assert r.status_code == 200
    assert "$120.00" in r.text


def test_range_too_large_rejected(client, engine):
    _fix(engine)
    r = _get(client, "income", "2015-01-01", "2026-01-01")
    assert r.status_code == 200
    assert "range too large" in r.text.lower()
    assert "$120.00" not in r.text


def test_blank_range_defaults_to_current_month(client, engine):
    r = client.get("/analytics")
    assert r.status_code == 200


def test_unparsable_range_defaults_to_current_month(client, engine):
    r = _get(client, "income", "not-a-date", "also-not-a-date")
    assert r.status_code == 200

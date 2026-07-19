import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
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


# --- Rates tab ops: bulk raise preview/apply --------------------------------

def _tier_cents(engine):
    with Session(engine) as s:
        return sorted(t.rate_cents for t in s.scalars(select(models.RateTier)).all())


def test_preview_shows_old_new_math_without_mutating(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/preview", data={"percent": "10", "scope_tiers": "on"})
    assert r.status_code == 200
    assert "$49.50" in r.text  # 4500 tier + 10% = 4950 cents
    assert _tier_cents(engine) == [4500, 5500, 7000]  # untouched


def test_preview_overrides_scope(client, engine):
    _fix(engine)
    with Session(engine) as s:
        pa = s.scalars(select(models.Property)).first()
        pa.rate_override_cents = 4000
        s.commit()
    r = client.post("/analytics/rates/preview", data={"percent": "10", "scope_overrides": "on"})
    assert r.status_code == 200
    assert "$44.00" in r.text  # 4000 + 10% = 4400 cents
    with Session(engine) as s:
        pa = s.scalars(select(models.Property)).first()
        assert pa.rate_override_cents == 4000  # untouched


def test_apply_both_scopes_updates_and_logs(client, engine):
    _fix(engine)
    with Session(engine) as s:
        pa = s.scalars(select(models.Property)).first()
        pa.rate_override_cents = 4000
        s.commit()
        pa_id = pa.id
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id

    r = client.post("/analytics/rates/apply",
                    data={"percent": "10", "scope_tiers": "on", "scope_overrides": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "tab=rates&applied=4" in r.headers["location"]

    assert _tier_cents(engine) == [4950, 6050, 7700]
    with Session(engine) as s:
        prop = s.get(models.Property, pa_id)
        assert prop.rate_override_cents == 4400
        rc = s.scalars(select(models.RateChange)).one()
        assert rc.percent == 10 and rc.scope == "both" and rc.affected_count == 4
        assert rc.user_id == owner_id

    follow = client.get(r.headers["location"])
    assert follow.status_code == 200
    assert "Applied to 4 rates" in follow.text


def test_apply_tiers_only_leaves_overrides_untouched(client, engine):
    _fix(engine)
    with Session(engine) as s:
        pa = s.scalars(select(models.Property)).first()
        pa.rate_override_cents = 4000
        s.commit()
        pa_id = pa.id

    r = client.post("/analytics/rates/apply", data={"percent": "10", "scope_tiers": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "tab=rates&applied=3" in r.headers["location"]
    assert _tier_cents(engine) == [4950, 6050, 7700]
    with Session(engine) as s:
        prop = s.get(models.Property, pa_id)
        assert prop.rate_override_cents == 4000
        rc = s.scalars(select(models.RateChange)).one()
        assert rc.scope == "tiers" and rc.affected_count == 3


@pytest.mark.parametrize("bad", ["abc", "0", "150", "", "-5"])
def test_apply_rejects_bad_percent(client, engine, bad):
    _fix(engine)
    r = client.post("/analytics/rates/apply", data={"percent": bad, "scope_tiers": "on"})
    assert r.status_code == 200
    assert "Percent must be between 0 and 100" in r.text
    assert _tier_cents(engine) == [4500, 5500, 7000]
    with Session(engine) as s:
        assert s.scalars(select(models.RateChange)).first() is None


def test_preview_rejects_bad_percent(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/preview", data={"percent": "abc", "scope_tiers": "on"})
    assert r.status_code == 200
    assert "Percent must be between 0 and 100" in r.text
    assert _tier_cents(engine) == [4500, 5500, 7000]


def test_apply_rejects_no_scope(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/apply", data={"percent": "10"})
    assert r.status_code == 200
    assert "Pick at least one scope" in r.text
    assert _tier_cents(engine) == [4500, 5500, 7000]
    with Session(engine) as s:
        assert s.scalars(select(models.RateChange)).first() is None


def test_preview_rejects_no_scope(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/preview", data={"percent": "10"})
    assert r.status_code == 200
    assert "Pick at least one scope" in r.text


def test_rate_ops_tech_forbidden(client, engine, tech_client):
    _fix(engine)
    t = tech_client(name="notarateadmin")
    assert t.post("/analytics/rates/preview",
                  data={"percent": "10", "scope_tiers": "on"}).status_code == 403
    assert t.post("/analytics/rates/apply",
                  data={"percent": "10", "scope_tiers": "on"}).status_code == 403


def test_apply_midway_failure_rolls_back(client, engine, monkeypatch):
    _fix(engine)
    from app import models as m

    class Boom(Exception):
        pass

    def explode(*a, **k):
        raise Boom("mid-apply")

    monkeypatch.setattr("app.routers.analytics.models.RateChange", explode)
    with pytest.raises(Boom):
        client.post("/analytics/rates/apply",
                    data={"percent": "10", "scope_tiers": "on"})
    with Session(engine) as s:
        tiers = sorted(t.rate_cents for t in s.scalars(select(m.RateTier)).all())
        assert tiers == [4500, 5500, 7000]  # nothing committed


def test_apply_overrides_skips_inactive(client, engine):
    _fix(engine)
    with Session(engine) as s:
        a = models.Customer(name="ActiveOv")
        b = models.Customer(name="InactiveOv")
        pa = models.Property(customer=a, rate_override_cents=4000)
        pb = models.Property(customer=b, rate_override_cents=3000, active=False)
        s.add_all([pa, pb])
        s.commit()
        pid_a, pid_b = pa.id, pb.id
    client.post("/analytics/rates/apply",
                data={"percent": "10", "scope_overrides": "on"})
    with Session(engine) as s:
        assert s.get(models.Property, pid_a).rate_override_cents == 4400
        assert s.get(models.Property, pid_b).rate_override_cents == 3000

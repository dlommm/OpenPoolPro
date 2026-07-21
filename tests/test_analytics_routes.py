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
    assert _tier_cents(engine) == [2000, 3000, 4500, 5500, 7000]  # untouched


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
    assert "tab=rates&applied=6" in r.headers["location"]

    assert _tier_cents(engine) == [2200, 3300, 4950, 6050, 7700]
    with Session(engine) as s:
        prop = s.get(models.Property, pa_id)
        assert prop.rate_override_cents == 4400
        rc = s.scalars(select(models.RateChange)).one()
        assert rc.percent == 10 and rc.scope == "both" and rc.affected_count == 6
        assert rc.user_id == owner_id

    follow = client.get(r.headers["location"])
    assert follow.status_code == 200
    assert "Applied to 6 rates" in follow.text


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
    assert "tab=rates&applied=5" in r.headers["location"]
    assert _tier_cents(engine) == [2200, 3300, 4950, 6050, 7700]
    with Session(engine) as s:
        prop = s.get(models.Property, pa_id)
        assert prop.rate_override_cents == 4000
        rc = s.scalars(select(models.RateChange)).one()
        assert rc.scope == "tiers" and rc.affected_count == 5


@pytest.mark.parametrize("bad", ["abc", "0", "150", "", "-5"])
def test_apply_rejects_bad_percent(client, engine, bad):
    _fix(engine)
    r = client.post("/analytics/rates/apply", data={"percent": bad, "scope_tiers": "on"})
    assert r.status_code == 200
    assert "Percent must be between 0 and 100" in r.text
    assert _tier_cents(engine) == [2000, 3000, 4500, 5500, 7000]
    with Session(engine) as s:
        assert s.scalars(select(models.RateChange)).first() is None


def test_preview_rejects_bad_percent(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/preview", data={"percent": "abc", "scope_tiers": "on"})
    assert r.status_code == 200
    assert "Percent must be between 0 and 100" in r.text
    assert _tier_cents(engine) == [2000, 3000, 4500, 5500, 7000]


def test_apply_rejects_no_scope(client, engine):
    _fix(engine)
    r = client.post("/analytics/rates/apply", data={"percent": "10"})
    assert r.status_code == 200
    assert "Pick at least one scope" in r.text
    assert _tier_cents(engine) == [2000, 3000, 4500, 5500, 7000]
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


def test_applied_non_int_ignored(client, engine):
    _fix(engine)
    r = client.get("/analytics", params={"tab": "rates", "applied": "notanumber"})
    assert r.status_code == 200
    assert "Applied to" not in r.text


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
        assert tiers == [2000, 3000, 4500, 5500, 7000]  # nothing committed


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


# --- CSV export tests -----

def test_income_export_golden(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "income",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().split("\n")
    assert lines[0] == "week_start,iso_week,labor,chemicals_cost,chemicals_billable,jobs,total"
    # Find week 28 and week 31 rows
    week28_row = None
    week31_row = None
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] == "28":
            week28_row = line
        elif len(parts) >= 2 and parts[1] == "31":
            week31_row = line
    assert week28_row is not None, "Week 28 row not found in export"
    # Week 28: labor 120.00, chemicals_cost 8.00, chemicals_billable 12.00, jobs 0.00, total 132.00
    assert "120.00" in week28_row, f"Labor amount not found in week 28 row: {week28_row}"
    assert "12.00" in week28_row, f"Billable amount not found in week 28 row: {week28_row}"
    assert "132.00" in week28_row, f"Total amount not found in week 28 row: {week28_row}"
    # Week 31: jobs 30.00
    assert week31_row is not None, "Week 31 row not found in export"
    parts = week31_row.split(",")
    assert parts[5] == "30.00", f"Week 31 jobs not 30.00, got: {week31_row}"


def test_customers_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "customers",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "customer,visits,labor,chemicals,jobs,total"


def test_crew_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "crew",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "tech,visits,jobs,minutes,revenue,revenue_per_hour"
    # Check that Unattributed is present
    assert any("Unattributed" in line for line in lines[1:])


def test_types_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "types",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "type,total"


def test_chemicals_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "chemicals",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "chemical,quantity,unit,cost,billable,margin"


def test_volume_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "volume",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "week_start,done,skipped,avg_minutes"


def test_rates_export_header(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "rates",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "customer,property,current,tier,delta"


def test_export_unknown_tab_404(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "bogus"})
    assert r.status_code == 404


def test_export_tech_forbidden(client, engine, tech_client):
    _fix(engine)
    t = tech_client(name="notadmin")
    r = t.get("/analytics/export", params={"tab": "income"})
    assert r.status_code == 403


def test_export_filename_correct(client, engine):
    _fix(engine)
    r = client.get("/analytics/export", params={"tab": "income",
                                                  "start": RANGE[0].isoformat(),
                                                  "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    disposition = r.headers.get("content-disposition", "")
    expected_filename = f"analytics-income-{RANGE[0].isoformat()}-{RANGE[1].isoformat()}.csv"
    assert expected_filename in disposition


def test_export_writes_export_analytics_audit_row(client, engine):
    _fix(engine)
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.get("/analytics/export", params={"tab": "income",
                                             "start": RANGE[0].isoformat(),
                                             "end": RANGE[1].isoformat()})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "export.analytics")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id
        assert "income" in rows[0].detail


def test_apply_writes_rates_bulk_raise_audit_row(client, engine):
    _fix(engine)
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.post("/analytics/rates/apply", data={"percent": "10", "scope_tiers": "on"})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.bulk_raise")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id


def test_bulk_raise_bumps_spa_overrides_and_spa_tiers(client, engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Dee")
        p = models.Property(customer=c, spa_rate_override_cents=2000)
        s.add(p)
        s.add(models.RateTier(max_gallons=600, rate_cents=2000, label="Spa", kind="spa"))
        s.commit()
    r = client.post("/analytics/rates/apply", data={
        "percent": "10", "scope_tiers": "true", "scope_overrides": "true"},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.scalars(select(models.Property).where(models.Property.customer.has(name="Dee"))).one()
        spa_tier = s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).first()
        assert p.spa_rate_override_cents == 2200   # 2000 * 1.10
        assert spa_tier.rate_cents == 2200

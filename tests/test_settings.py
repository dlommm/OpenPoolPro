from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, mailer, models


def test_update_chemical_cost(client, engine):
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical)).first()
        cid = chem.id
    client.post("/settings/chemicals", data={
        "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
        "cost_dollars": ["4.75"], f"active_{cid}": "on"})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).cost_per_unit_cents == 475


def test_add_and_edit_tier(client, engine):
    # /settings/tiers manages POOL tiers only; spa tiers must be untouched.
    with Session(engine) as s:
        tiers = s.scalars(
            select(models.RateTier).where(models.RateTier.kind == "pool").order_by(models.RateTier.id)
        ).all()
        ids = [str(t.id) for t in tiers]
    client.post("/settings/tiers", data={
        "tier_id": ids + [""],
        "max_gallons": ["15000", "25000", "", "40000"],
        "rate_dollars": ["48", "58", "75", "65"],
        "label": ["S", "M", "XL", "L"]})
    with Session(engine) as s:
        pool_tiers = s.scalars(select(models.RateTier).where(models.RateTier.kind == "pool")).all()
        assert len(pool_tiers) == 4
        by_label = {t.label: t for t in pool_tiers}
        assert by_label["S"].rate_cents == 4800
        assert by_label["L"].max_gallons == 40000


def test_tier_edit_writes_rates_change_audit_row(client, engine):
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier).order_by(models.RateTier.id)).all()
        ids = [str(t.id) for t in tiers]
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.post("/settings/tiers", data={
        "tier_id": ids + [""],
        "max_gallons": ["15000", "25000", "", "40000"],
        "rate_dollars": ["48", "58", "75", "65"],
        "label": ["S", "M", "XL", "L"]})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.change")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id


def test_tier_save_with_no_actual_change_skips_audit(client, engine):
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier).order_by(models.RateTier.id)).all()
        ids = [str(t.id) for t in tiers]
        rate_cents = [t.rate_cents for t in tiers]
        maxg = [str(t.max_gallons) if t.max_gallons is not None else "" for t in tiers]
        labels = [t.label for t in tiers]
    rates = [f"{c / 100:.2f}" for c in rate_cents]

    # Submitting nothing at all is a no-op.
    client.post("/settings/tiers", data={"tier_id": [], "max_gallons": [], "rate_dollars": [], "label": []})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.change")).all()
        assert len(rows) == 0

    # Resubmitting the exact same values for every existing row must ALSO be
    # a no-op -- every row is "touched" by the form post, but nothing about
    # any of them actually changed, so no rates.change row should appear.
    client.post("/settings/tiers", data={
        "tier_id": ids, "max_gallons": maxg, "rate_dollars": rates, "label": labels})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.change")).all()
        assert len(rows) == 0

    # A real change on top of that resubmission produces exactly one row.
    rates[0] = f"{(rate_cents[0] + 500) / 100:.2f}"
    client.post("/settings/tiers", data={
        "tier_id": ids, "max_gallons": maxg, "rate_dollars": rates, "label": labels})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "rates.change")).all()
        assert len(rows) == 1


def test_markup_setting(client, engine):
    client.post("/settings/general", data={"chem_markup_percent": "25",
                                           "business_name": "Coastline Pools"})
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "25"


def test_garbage_markup_does_not_500_money_page(client, engine):
    # I2 pin: a trailing "%" is tolerated and normalized; a value that
    # isn't a number at all is rejected and the previously stored value is
    # kept — either way /money must keep returning 200, never 500.
    client.post("/settings/general", data={"chem_markup_percent": "5%",
                                           "business_name": "Coastline Pools"})
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "5"
    assert client.get("/money").status_code == 200

    r = client.post("/settings/general", data={"chem_markup_percent": "abc",
                                                "business_name": "Coastline Pools"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "5"  # previous value retained
    assert client.get("/money").status_code == 200


def test_markup_non_finite_rejected(client, engine):
    # inf/nan markup would blow up round(cost * (1 + markup/100)) on the
    # Money page and every analytics tab — reject it like save_targets does.
    client.post("/settings/general", data={"chem_markup_percent": "10",
                                           "business_name": "Coastline Pools"})
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "10"

    r = client.post("/settings/general", data={"chem_markup_percent": "inf",
                                                "business_name": "Coastline Pools"},
                    follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "chem_markup_percent", "0") == "10"  # inf rejected
    assert client.get("/money").status_code == 200


def test_stale_and_garbage_rows_skipped(client, engine):
    client.post("/settings/tiers", data={"tier_id": ["9999", "abc"],
                                         "max_gallons": ["", ""],
                                         "rate_dollars": ["50", "60"],
                                         "label": ["Stale", "Garbage"]})
    with Session(engine) as s:
        labels = {t.label for t in s.scalars(select(models.RateTier)).all()}
        assert "Stale" not in labels and "Garbage" not in labels
    client.post("/settings/chemicals", data={
        "chem_id": ["9999", "abc"], "name": ["X", "Y"], "unit": ["gal", "gal"],
        "cost_dollars": ["1", "2"]})
    client.post("/settings/tasks", data={
        "task_id": ["9999", "abc"], "label": ["X", "Y"]})


def test_tier_delete_via_blank_rate(client, engine):
    with Session(engine) as s:
        tid = s.scalars(select(models.RateTier)).first().id
        before = len(s.scalars(select(models.RateTier)).all())
    client.post("/settings/tiers", data={"tier_id": [str(tid)], "max_gallons": [""],
                                         "rate_dollars": [""], "label": [""]})
    with Session(engine) as s:
        assert len(s.scalars(select(models.RateTier)).all()) == before - 1


def test_chemical_deactivate_via_blank_name(client, engine):
    with Session(engine) as s:
        cid = s.scalars(select(models.Chemical)).first().id
    client.post("/settings/chemicals", data={"chem_id": [str(cid)], "name": [""],
                                             "unit": ["gal"], "cost_dollars": ["0"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).active is False


def test_backup_download_route(client, engine):
    import os
    import app.config as config
    bdir = os.path.join(config.DATA_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "app-2026-07-01.db"), "wb") as f:
        f.write(b"data")
    assert client.get("/settings/backups/app-2026-07-01.db").status_code == 200
    assert client.get("/settings/backups/missing.db").status_code == 404
    assert client.get("/settings/backups/..%2fsecret").status_code in (400, 404)


def test_deactivate_ignores_malformed_cost(client, engine):
    with Session(engine) as s:
        cid = s.scalars(select(models.Chemical)).first().id
    client.post("/settings/chemicals", data={"chem_id": [str(cid)], "name": [""],
                                             "unit": ["gal"], "cost_dollars": ["abc"]})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).active is False


def test_stale_tier_blank_rate_skipped(client, engine):
    with Session(engine) as s:
        before = len(s.scalars(select(models.RateTier)).all())
    client.post("/settings/tiers", data={"tier_id": ["9999"], "max_gallons": [""],
                                         "rate_dollars": [""], "label": [""]})
    with Session(engine) as s:
        assert len(s.scalars(select(models.RateTier)).all()) == before


def test_mixed_batch_legit_rows_survive(client, engine):
    client.post("/settings/tiers", data={
        "tier_id": ["abc", ""], "max_gallons": ["", "50000"],
        "rate_dollars": ["60", "80"], "label": ["Bad", "Good"]})
    with Session(engine) as s:
        labels = {t.label for t in s.scalars(select(models.RateTier)).all()}
        assert "Good" in labels and "Bad" not in labels
    client.post("/settings/chemicals", data={
        "chem_id": ["zzz", ""], "name": ["Ghost", "Borax"],
        "unit": ["gal", "lb"], "cost_dollars": ["1", "2"]})
    with Session(engine) as s:
        names = {c.name for c in s.scalars(select(models.Chemical)).all()}
        assert "Borax" in names and "Ghost" not in names


def test_targets_save_and_tolerant(client, engine):
    r = client.post("/settings/targets", data={
        "target_fc": "5.0", "target_ph": "7.4", "target_ta": "100",
        "target_cya": "60", "target_salt": "3400"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "target_fc", "4.0") == "5.0"
        assert models.get_setting(s, "target_ph", "7.5") == "7.4"
    client.post("/settings/targets", data={
        "target_fc": "abc", "target_ph": "7.6", "target_ta": "100",
        "target_cya": "60", "target_salt": "3400"})
    with Session(engine) as s:
        assert models.get_setting(s, "target_fc", "4.0") == "5.0"  # kept previous
        assert models.get_setting(s, "target_ph", "7.5") == "7.6"


def test_targets_admin_only(client, tech_client):
    t = tech_client(name="targetless")
    assert t.post("/settings/targets", data={"target_fc": "9"},
                  follow_redirects=False).status_code == 403


def test_target_non_finite_rejected(client, engine):
    client.post("/settings/targets", data={
        "target_fc": "5.0", "target_ph": "7.4", "target_ta": "100",
        "target_cya": "60", "target_salt": "3400"})
    with Session(engine) as s:
        assert models.get_setting(s, "target_fc", "4.0") == "5.0"
    client.post("/settings/targets", data={
        "target_fc": "inf", "target_ph": "7.6", "target_ta": "100",
        "target_cya": "60", "target_salt": "3400"})
    with Session(engine) as s:
        assert models.get_setting(s, "target_fc", "4.0") == "5.0"  # inf rejected, kept previous
    client.post("/settings/targets", data={
        "target_fc": "1e999", "target_ph": "7.6", "target_ta": "100",
        "target_cya": "60", "target_salt": "3400"})
    with Session(engine) as s:
        assert models.get_setting(s, "target_fc", "4.0") == "5.0"  # 1e999 -> inf, rejected


def test_chemical_role_strength_editable(client, engine):
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        cid = chem.id
    client.post("/settings/chemicals", data={
        "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
        "cost_dollars": ["4.50"], "role": ["chlorine_granular"],
        "strength_pct": ["10"], f"active_{cid}": "on"})
    with Session(engine) as s:
        c = s.get(models.Chemical, cid)
        assert c.role == "chlorine_granular" and c.strength_pct == 10.0
    # unknown role value coerced to other; garbage strength keeps previous row values
    client.post("/settings/chemicals", data={
        "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
        "cost_dollars": ["4.50"], "role": ["evil_role"],
        "strength_pct": ["10"], f"active_{cid}": "on"})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).role == "other"


def test_chemical_strength_non_finite_or_nonpositive_rejected(client, engine):
    # I1 pin: strength_pct must stay a usable divisor — 0, negative, and
    # non-finite values are rejected and the previous value is kept.
    with Session(engine) as s:
        chem = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Liquid chlorine")).one()
        cid = chem.id
    client.post("/settings/chemicals", data={
        "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
        "cost_dollars": ["4.50"], "role": ["chlorine_liquid"],
        "strength_pct": ["12.5"], f"active_{cid}": "on"})
    with Session(engine) as s:
        assert s.get(models.Chemical, cid).strength_pct == 12.5
    for bad in ["0", "-5", "inf"]:
        client.post("/settings/chemicals", data={
            "chem_id": [str(cid)], "name": ["Liquid chlorine"], "unit": ["gal"],
            "cost_dollars": ["4.50"], "role": ["chlorine_liquid"],
            "strength_pct": [bad], f"active_{cid}": "on"})
        with Session(engine) as s:
            assert s.get(models.Chemical, cid).strength_pct == 12.5  # rejected, kept previous


def test_new_chemical_defaults_role_other(client, engine):
    client.post("/settings/chemicals", data={
        "chem_id": [""], "name": ["Mystery Goo"], "unit": ["gal"],
        "cost_dollars": ["3"], "role": ["other"], "strength_pct": [""]})
    with Session(engine) as s:
        c = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Mystery Goo")).one()
        assert c.role == "other" and c.strength_pct == 100.0
    html = client.get("/settings").text
    import re
    new_row_selects = re.findall(r'<select name="role"[^>]*>(.*?)</select>', html, re.S)
    assert any('selected' in s and '>other<' in s for s in new_row_selects)


def test_run_now_admin_creates_job_and_shows_banner(client, engine):
    from app.routers.today import business_today
    today = business_today()
    with Session(engine) as s:
        c = models.Customer(name="RunNow Cust", active=True)
        p = models.Property(customer=c, active=True)
        s.add(p)
        s.commit()
        s.add(models.MaintenanceRule(property_id=p.id, title="Run now rule",
                                     next_due=today, lead_days=0, interval_weeks=4))
        s.commit()

    r = client.post("/settings/maintenance/run-now", follow_redirects=False)
    assert r.status_code == 303
    assert "rules_created=1" in r.headers["location"]

    html = client.get(r.headers["location"]).text
    assert "Created 1 maintenance job" in html


def test_run_now_tech_forbidden(client, tech_client):
    t = tech_client(name="runnowtech")
    assert t.post("/settings/maintenance/run-now",
                  follow_redirects=False).status_code == 403


# --- Email section -------------------------------------------------------------

def test_settings_shows_unconfigured_badge_by_default(client, engine):
    html = client.get("/settings").text
    assert "Not configured" in html


def test_settings_shows_configured_badge_with_host_only(client, engine, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "user@example.com")
    monkeypatch.setattr(config, "SMTP_PASS", "supersecret")
    html = client.get("/settings").text
    assert "Configured" in html and "smtp.example.com" in html
    assert "supersecret" not in html  # credentials never render


def test_settings_outbox_lists_recent_rows(client, engine):
    with Session(engine) as s:
        mailer.queue(s, "cust@x.com", "Reminder", "Body text")
        s.commit()
    html = client.get("/settings").text
    assert "cust@x.com" in html and "Reminder" in html and "pending" in html


def test_send_test_email_blank_address_errors_and_queues_nothing(client, engine):
    r = client.post("/settings/mail/test", data={"to_addr": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "mail_error" in r.headers["location"]
    html = client.get(r.headers["location"]).text
    assert "valid email address" in html
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_send_test_email_no_at_sign_errors_and_queues_nothing(client, engine):
    r = client.post("/settings/mail/test", data={"to_addr": "not-an-email"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "mail_error" in r.headers["location"]
    with Session(engine) as s:
        assert s.scalars(select(models.EmailOutbox)).all() == []


def test_send_test_email_queues_and_flushes(client, engine, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "send_message", lambda *a: None)
    r = client.post("/settings/mail/test", data={"to_addr": "dest@example.com"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "mail_sent=1" in r.headers["location"] and "mail_failed=0" in r.headers["location"]
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.to_addr == "dest@example.com" and row.status == "sent"
    html = client.get(r.headers["location"]).text
    assert "1 sent" in html and "0 failed" in html


def test_flush_now_shows_banner(client, engine, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "send_message", lambda *a: None)
    with Session(engine) as s:
        mailer.queue(s, "a@x.com", "s", "b")
        mailer.queue(s, "b@x.com", "s", "b")
        s.commit()
    r = client.post("/settings/mail/flush", follow_redirects=False)
    assert r.status_code == 303
    html = client.get(r.headers["location"]).text
    assert "2 sent" in html and "0 failed" in html


def test_settings_email_section_admin_only(tech_client):
    t = tech_client(name="mailtech")
    assert t.get("/settings").status_code == 403
    assert t.post("/settings/mail/flush", follow_redirects=False).status_code == 403
    assert t.post("/settings/mail/test", data={"to_addr": "x@x.com"},
                  follow_redirects=False).status_code == 403


def test_mail_result_banner_requires_both_counts(client, engine):
    # mail_sent set but mail_failed absent (half-set pair) must not render
    # a "Flushed" banner at all — both counts must parse for it to show.
    html = client.get("/settings?mail_sent=3").text
    assert "Flushed" not in html
    assert "None" not in html


def test_save_spa_tiers(client, engine):
    r = client.post("/settings/spa_tiers", data={
        "tier_id": ["", ""], "max_gallons": ["600", ""],
        "rate_dollars": ["18.00", "26.00"],
        "label": ["Spa small", "Spa large"]}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        spa = s.scalars(select(models.RateTier).where(models.RateTier.kind == "spa")).all()
        assert {t.label for t in spa} >= {"Spa small", "Spa large"}
        assert all(t.kind == "spa" for t in spa)

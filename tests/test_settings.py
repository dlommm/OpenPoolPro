from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


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
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier).order_by(models.RateTier.id)).all()
        ids = [str(t.id) for t in tiers]
    client.post("/settings/tiers", data={
        "tier_id": ids + [""],
        "max_gallons": ["15000", "25000", "", "40000"],
        "rate_dollars": ["48", "58", "75", "65"],
        "label": ["S", "M", "XL", "L"]})
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier)).all()
        assert len(tiers) == 4
        by_label = {t.label: t for t in tiers}
        assert by_label["S"].rate_cents == 4800
        assert by_label["L"].max_gallons == 40000


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

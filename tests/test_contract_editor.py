"""The three-pane contract editor (Settings > Contract): its no-JS form
fallback, the sample preview's spa toggle and built-in blocks, and the
follow-up rule (days before a sent contract counts as unsigned-and-stale)."""
import json

import pytest
from sqlalchemy.orm import Session

from app import models
from app.contract_defaults import DEFAULT_TEMPLATE, TEMPLATE_KEY, get_stale_days
from app.db import Base


@pytest.fixture(autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)


def test_contract_tab_three_panes_and_no_js_fallback(client):
    """Outline | page | inspector, with the no-JS form intact: every text
    section ships its real title/body inputs, the Save button is a plain
    submit, and the field-code reference is server-rendered."""
    t = client.get("/settings").text
    assert 'class="card ce-outline"' in t and 'id="ct-doc"' in t and 'id="ct-insp"' in t
    assert 'href="#ct-b-1"' in t                       # outline works without JS
    n_text = sum(1 for b in DEFAULT_TEMPLATE["blocks"] if b["kind"] == "text")
    assert t.count('name="body"') == n_text + 1        # + the add-section <template>
    assert 'id="ct-insp-nojs"' in t and "{{customer_name}}" in t
    assert '<button class="btn pri">Save contract</button>' in t
    assert 'data-ct-token="weekly_price"' in t         # JS chip palette
    assert 'nonce="' in t.split('id="ct-root"', 1)[1]  # editor script is nonced


def test_contract_tab_marks_spa_only_and_builtin_blocks(client, engine):
    with Session(engine) as s:
        models.set_setting(s, TEMPLATE_KEY, json.dumps({"title": "T", "blocks": [
            {"kind": "text", "title": "Spa Care", "body": "x", "when": "includes_spa"},
            {"kind": "rates", "visible": False},
        ]}))
        s.commit()
    t = client.get("/settings").text
    assert '<span class="ce-tag">Spa</span>' in t
    assert '<span class="ce-tag off">Hidden</span>' in t
    assert '<input type="hidden" name="visible" value="off">' in t
    assert '<input type="hidden" name="when" value="includes_spa">' in t


def _preview(client, **extra):
    data = {"doc_title": "Agreement", "kind": ["text", "text", "parties", "rates", "signature"],
            "title": ["Scope", "Spa Care"], "body": ["Weekly {{weekly_price}}.", "Spa body."],
            "when": ["", "includes_spa"], "visible": ["on", "on", "on"], **extra}
    r = client.post("/settings/contract/preview", data=data)
    assert r.status_code == 200
    return r.text


def test_preview_spa_toggle_drops_spa_only_sections(client):
    assert "Spa Care" in _preview(client)
    assert "Spa Care" in _preview(client, ct_sample_spa="1")
    pool_only = _preview(client, ct_sample_spa="0")
    assert "Spa Care" not in pool_only and "Scope" in pool_only


def test_preview_renders_builtin_blocks_with_sample_data(client):
    t = _preview(client)
    assert "Sample Customer" in t                   # parties block
    assert "$85.00/week" in t                       # rates block
    assert 'class="ce-sig"' in t                    # signature lines
    assert "[parties block]" not in t


def test_preview_hides_builtin_blocks_switched_off(client):
    t = _preview(client, visible=["off", "off", "off"])
    assert "Sample Customer" not in t and 'class="ce-sig"' not in t


def test_preview_uses_company_details_when_set(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "business_name", "Bluewater Test Pools")
        s.commit()
    data = {"doc_title": "Agreement", "kind": ["text"], "title": [""],
            "body": ["By {{company_name}}."], "visible": []}
    t = client.post("/settings/contract/preview", data=data).text
    assert "By Bluewater Test Pools." in t


def test_preview_escapes_markup(client):
    t = _preview(client, title=["<script>x</script>", "Spa Care"])
    assert "<script>x</script>" not in t and "&lt;script&gt;" in t


def test_rules_save_and_validation(client, engine):
    with Session(engine) as s:
        assert get_stale_days(s) == 14
    r = client.post("/settings/contract/rules", data={"stale_days": "21"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#contract"
    with Session(engine) as s:
        assert get_stale_days(s) == 21
    for bad in ("0", "91", "abc", "", "7.5"):
        r = client.post("/settings/contract/rules", data={"stale_days": bad},
                        follow_redirects=False)
        assert r.status_code == 400, bad
    with Session(engine) as s:
        assert get_stale_days(s) == 21
    assert 'value="21"' in client.get("/settings").text


def test_rules_requires_admin(tech_client):
    c = tech_client("sam")
    r = c.post("/settings/contract/rules", data={"stale_days": "10"}, follow_redirects=False)
    assert r.status_code == 403


def test_stale_days_falls_back_on_bad_stored_value(engine):
    with Session(engine) as s:
        models.set_setting(s, "contract_stale_days", "0")
        assert get_stale_days(s) == 14
        models.set_setting(s, "contract_stale_days", "junk")
        assert get_stale_days(s) == 14

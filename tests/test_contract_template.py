import io
import json
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app import contract_pdf, models
from app.contract_defaults import DEFAULT_TEMPLATE, get_template, TEMPLATE_KEY
from app.db import Base


@pytest.fixture(autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)


# --- Task 2: default template + loader ------------------------------------
def test_default_template_shape():
    assert DEFAULT_TEMPLATE["title"]
    kinds = [b["kind"] for b in DEFAULT_TEMPLATE["blocks"]]
    assert "text" in kinds and "rates" in kinds and "signature" in kinds
    assert any("{{pool_label}}" in b.get("body", "") for b in DEFAULT_TEMPLATE["blocks"])


def test_get_template_returns_default_when_unset(engine):
    with Session(engine) as s:
        assert get_template(s)["title"] == DEFAULT_TEMPLATE["title"]


def test_get_template_returns_stored(engine):
    custom = {"title": "My Agreement", "blocks": [{"kind": "text", "title": "T", "body": "hi"}]}
    with Session(engine) as s:
        models.set_setting(s, TEMPLATE_KEY, json.dumps(custom)); s.commit()
        assert get_template(s) == custom


def test_get_template_falls_back_on_corrupt_json(engine):
    with Session(engine) as s:
        models.set_setting(s, TEMPLATE_KEY, "{not json"); s.commit()
        assert get_template(s)["title"] == DEFAULT_TEMPLATE["title"]


# --- Task 3: render the PDF from the template -----------------------------
def _contract_fixtures():
    contract = SimpleNamespace(weekly_cents=8500, spa_cents=6000, open_close_cents=40000,
                               green_clean_cents=12000, storm_cents=9000, service_call_cents=15000,
                               weeks=32, includes_spa=False)
    pool = SimpleNamespace(label="Main", gallons=18432, water_type="salt", surface="plaster",
                           length_ft=16, width_ft=32, avg_depth_ft=5)
    customer = SimpleNamespace(name="Marcus Alvarez")
    prop = SimpleNamespace(label="Home", address="88 Gulfshore Blvd", gate_code="4417",
                           city="Cocoa Beach", state="FL", zip="32931")
    company = {"business_name": "Coastline Pools"}
    return contract, pool, prop, customer, company


def _pdf_text(pdf_bytes):
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def test_default_render_has_current_sections(engine):
    contract, pool, prop, customer, company = _contract_fixtures()
    with Session(engine) as s:
        pdf = contract_pdf.render_contract(contract, pool, prop, customer, company, session=s)
    t = _pdf_text(pdf)
    assert "Scope of Weekly Pool Service" in t
    assert "Payment Terms" in t
    assert "Coastline Pools" in t


def test_custom_added_section_appears(engine):
    contract, pool, prop, customer, company = _contract_fixtures()
    custom = {"title": "Agreement", "blocks": [
        {"kind": "text", "title": "Cancellation Policy",
         "body": "Either party may cancel with 30 days notice to {{company_name}}."}]}
    with Session(engine) as s:
        models.set_setting(s, TEMPLATE_KEY, json.dumps(custom)); s.commit()
        pdf = contract_pdf.render_contract(contract, pool, prop, customer, company, session=s)
    t = _pdf_text(pdf)
    assert "Cancellation Policy" in t and "30 days notice to Coastline Pools" in t


# --- Task 4: editor save + preview routes ---------------------------------
def test_save_persists_template_admin(client, engine):
    r = client.post("/settings/contract/save", data={
        "doc_title": "Service Agreement",
        "kind": ["text", "rates", "signature"],
        "title": ["Intro"],
        "body": ["Welcome to {{company_name}}."],
        "visible": ["on", "on"],
    }, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        tpl = get_template(s)
        assert tpl["title"] == "Service Agreement"
        assert tpl["blocks"][0] == {"kind": "text", "title": "Intro",
                                    "body": "Welcome to {{company_name}}.", "when": None}
        assert tpl["blocks"][1]["kind"] == "rates" and tpl["blocks"][1]["visible"] is True


def test_save_requires_admin(tech_client):
    c = tech_client("sam")
    r = c.post("/settings/contract/save", data={"doc_title": "x", "kind": ["text"],
               "title": ["T"], "body": ["b"], "visible": []}, follow_redirects=False)
    assert r.status_code == 403


def test_preview_resolves_tokens(client):
    r = client.post("/settings/contract/preview", data={
        "doc_title": "Agreement", "kind": ["text"], "title": ["Scope"],
        "body": ["Billed at {{weekly_price}} by {{company_name}}."], "visible": []})
    assert r.status_code == 200
    assert "{{weekly_price}}" not in r.text and "Scope" in r.text

"""The block email builder: editable templates for the visit, invoice,
reminder and follow-up emails. Defaults reproduce the original wording byte
for byte; both parts come from one block list; text is escaped; fields come
only from each template's allow-list; the admin page works as plain forms."""
import io
import json
import smtplib
from datetime import date

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, email_brand, email_templates, mailer, models
from app.email_templates import clean, default_doc, fill, render, unknown_tokens

# --- Helpers ----------------------------------------------------------------


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_FROM", "office@example.com")
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP.sent


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    return tmp_path


def _png(w=40, h=20, colour=(20, 120, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def _visit(**kw):
    base = dict(business="Bluewater Pools", property_label="5 Port Royal Ln",
                day=date(2026, 10, 3), tech_name="Jordan Reyes",
                readings=[("pH", "7.5", "target 7.5"), ("Free chlorine", "3 ppm", "")],
                chemicals=[("Liquid chlorine", "1 gal")], tasks=["Brushed walls"],
                notes="Pump is loud.", signoff="")
    return email_brand.visit_mail(**{**base, **kw})


def _invoice(**kw):
    base = dict(company_name="Bluewater Pools", customer_name="Ada Rivera", number="1080",
                period="September 2026", total_cents=56000, due=date(2026, 10, 31))
    return email_brand.invoice_mail(**{**base, **kw})


def _reminder(**kw):
    base = dict(company_name="Bluewater Pools", customer_name="Ada Rivera", number="1049",
                balance_cents=61200, days_overdue=12)
    return email_brand.reminder_mail(**{**base, **kw})


def _followup(**kw):
    base = dict(company_name="Bluewater Pools", customer_name="Ada Rivera", number="1049",
                balance_cents=61200, due=None)
    return email_brand.followup_mail(**{**base, **kw})


def _html(mail, **kw):
    return email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Bluewater Pools", "phone": ""}, **kw)


def form_for(doc, **extra):
    """The builder form fields for a document, as the page would post them."""
    data = {"subject": doc["subject"], "es_mode": "palette", "es_button": "rounded",
            "order": ",".join(str(i) for i in range(len(doc["blocks"])))}
    for i, b in enumerate(doc["blocks"]):
        p = f"b{i}_"
        data[p + "type"] = b["type"]
        data[p + "show"] = b.get("show", "both")
        for k in ("text", "align", "link", "title", "image", "alt"):
            if k in b:
                data[p + k] = b[k]
        if b.get("rows"):
            data[p + "rl"] = [r[0] for r in b["rows"]]
            data[p + "rf"] = [r[1] for r in b["rows"]]
    data.update(extra)
    return data


def _saved(engine, key):
    with Session(engine) as s:
        raw = models.get_setting(s, f"email_tpl_{key}", "")
        return json.loads(raw) if raw else None


# --- Defaults: exactly the original emails ---------------------------------

def test_visit_default_plain_text_is_unchanged():
    m = _visit(signoff="Thanks!\nThe Team")
    assert m.subject == "Bluewater Pools — service visit completed"
    assert m.text == (
        "Bluewater Pools\n5 Port Royal Ln — October 3, 2026\n\n"
        "Water test:\n  pH: 7.5 (target 7.5)\n  Free chlorine: 3 ppm\n\n"
        "Chemicals added:\n  Liquid chlorine: 1 gal\n\n"
        "Completed:\n  Brushed walls\n\n"
        "Notes:\n  Pump is loud.\n\n"
        "Serviced by: Jordan Reyes\n\nThanks!\nThe Team")


def test_visit_default_without_details_or_signoff():
    m = _visit(readings=[], chemicals=[], tasks=[], notes="")
    assert m.text == "Bluewater Pools\n5 Port Royal Ln — October 3, 2026\n\nServiced by: Jordan Reyes"
    html = _html(m)
    assert "Your service visit is complete" in html
    assert "Jordan Reyes finished your service visit at 5 Port Royal Ln on October 3, 2026." in html
    assert "Notes from your technician" not in html and "Water test" not in html
    # The HTML closes with the company name when no sign-off is set.
    assert html.count("Bluewater Pools") >= 3


def test_visit_default_html_order():
    html = _html(_visit(notes="Pump is loud."))
    order = ["Your service visit is complete", "finished your service visit",
             "Notes from your technician: Pump is loud.", "Water test", "Chemicals added",
             "Completed", "Brushed walls"]
    pos = [html.index(s) for s in order]
    assert pos == sorted(pos)
    assert "Serviced by" not in html  # plain-text-only line


@pytest.mark.parametrize("due, line", [
    (date(2026, 10, 31), "Attached is invoice 1080 for September 2026, totalling $560.00, due 2026-10-31."),
    (None, "Attached is invoice 1080 for September 2026, totalling $560.00."),
])
def test_invoice_default_plain_text_is_unchanged(due, line):
    m = _invoice(due=due)
    assert m.subject == "Invoice 1080 — September 2026"
    assert m.text == f"Hello Ada Rivera,\n\n{line}\n\nThank you for your business.\n\nBluewater Pools"
    assert m.attachment == "1080.pdf"
    html = _html(m)
    assert "Your September 2026 invoice" in html and "Attached: <b" in html
    assert ("October 31, 2026" in html) == (due is not None)


@pytest.mark.parametrize("days, tail", [
    (0, " "), (1, " It is now 1 day past due."), (12, " It is now 12 days past due.")])
def test_reminder_default_plain_text_is_unchanged(days, tail):
    m = _reminder(days_overdue=days, signoff="The Office")
    assert m.subject == "Reminder: invoice 1049"
    assert m.text == ("Hello Ada Rivera,\n\nInvoice 1049 has an outstanding balance of $612.00."
                      f"{tail}\n\nIf you have already sent payment, thank you — please disregard "
                      "this note.\n\nThe Office")
    assert ("Days past due" in _html(m)) == bool(days)


@pytest.mark.parametrize("due, text", [(None, "on receipt"), (date(2026, 9, 1), "September 1, 2026")])
def test_followup_default_plain_text_is_unchanged(due, text):
    m = _followup(due=due)
    assert m.subject == "Bluewater Pools — reminder for invoice 1049"
    assert m.text == (
        f"Hello Ada Rivera,\n\nOur records show $612.00 outstanding on invoice 1049, due {text}.\n\n"
        "If you have already paid, thank you — please reply with the payment details so we can "
        "update our records. For payment arrangements or questions, please reply to this email."
        "\n\nBluewater Pools")
    assert "Was due" in _html(m)


def test_every_default_document_is_valid_and_round_trips():
    for key in email_templates.TEMPLATES:
        doc, errors = clean(key, default_doc(key))
        assert errors == [] and doc == email_templates.TEMPLATES[key].default


def test_saving_the_default_stores_nothing(engine):
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        email_templates.save(s, "invoice", default_doc("invoice"))
        assert not email_templates.is_edited(s, "invoice")


# --- Tokens ----------------------------------------------------------------

def test_fill_known_unknown_and_brackets():
    vals = {"customer_name": "Ada", "notes": "", "balance": "$5.00"}
    assert fill("Hi {customer_name}, {nope} {Customer_Name}", vals) == "Hi Ada, {nope} {Customer_Name}"
    assert fill("A[ — notes: {notes}]B", vals) == "AB"
    assert fill("Owe [{balance} now]", vals) == "Owe $5.00 now"
    assert fill("[plain brackets] [{nope}]", vals) == "[plain brackets] [{nope}]"
    # Values are inserted once, never re-read as fields or brackets.
    assert fill("{customer_name}", {"customer_name": "{balance} [x]"}) == "{balance} [x]"


def test_unknown_tokens_are_flagged():
    fields = email_templates.TEMPLATES["invoice"].fields
    assert unknown_tokens("Hi {customer_name} {total} {totl} {due date}", fields) == ["totl"]
    doc = default_doc("invoice")
    doc["subject"] = "Invoice {numbr}"
    doc["blocks"][1]["text"] = "Hello {first_name},"
    assert email_templates.warnings("invoice", doc) == {"subject": ["numbr"], 1: ["first_name"]}


def test_fields_from_another_template_are_not_filled():
    doc = default_doc("visit")
    doc["blocks"].insert(0, {"type": "text", "show": "both", "text": "Invoice {invoice_number}",
                             "align": "left"})
    m = _visit(doc=doc)
    assert m.text.startswith("Invoice {invoice_number}\n\n")


# --- One document, both parts ----------------------------------------------

def test_custom_document_drives_both_parts():
    doc = {"subject": "Hi {customer_name}", "blocks": [
        {"type": "heading", "show": "both", "text": "About {invoice_number}", "align": "center"},
        {"type": "text", "show": "both", "text": "Line one\nLine two\n\nSecond para", "align": "left"},
        {"type": "details", "show": "both", "title": "Summary",
         "rows": [["Owed", "balance"], ["Late by", "days_overdue"]]},
        {"type": "divider", "show": "both"},
        {"type": "text", "show": "html", "text": "Branded only", "align": "left"},
        {"type": "text", "show": "text", "text": "Plain only", "align": "left"},
        {"type": "signoff", "show": "both"},
    ]}
    m = _reminder(days_overdue=0, doc=doc, signoff="Cheers")
    assert m.subject == "Hi Ada Rivera"
    assert m.text == ("About 1049\n\nLine one\nLine two\n\nSecond para\n\n"
                      "Summary:\n  Owed: $612.00\n\n----\n\nPlain only\n\nCheers")
    html = _html(m)
    assert "text-align:center;\">About 1049</h1>" in html
    assert "Line one<br>Line two</p>" in html and html.count("Second para") == 1
    assert "Branded only" in html and "Plain only" not in html
    assert "Late by" not in html  # empty value -> row left out


def test_block_text_is_escaped_in_html():
    doc = default_doc("invoice")
    doc["blocks"][1]["text"] = "Hello <b>{customer_name}</b> javascript:alert(1) https://ok.example/x"
    html = _html(_invoice(customer_name='<img src=x onerror="1">', doc=doc))
    assert "<img src=x" not in html and "&lt;img src=x onerror=&#34;1&#34;&gt;" in html
    assert "&lt;b&gt;" in html and "<b>&lt;img" not in html
    assert 'href="https://ok.example/x"' in html and 'href="javascript' not in html


def test_button_needs_a_template_link():
    # None of the four templates has a URL today, so none offers a button.
    for key in email_templates.TEMPLATES:
        assert "button" not in email_templates.TEMPLATES[key].types
    tpl = email_templates.Template("t", "T", "", {"x": "X"}, {"subject": "s", "blocks": []},
                                   links={"portal_url": "Portal"})
    assert "button" in tpl.types


def test_rendered_button_from_links(monkeypatch):
    tpl = email_templates.Template("demo", "Demo", "", {"x": "X"},
                                   {"subject": "s", "blocks": []}, links={"portal_url": "Portal"})
    monkeypatch.setitem(email_templates.TEMPLATES, "demo", tpl)
    doc = {"subject": "s", "blocks": [{"type": "button", "show": "both", "text": "Open {x}",
                                       "align": "left", "link": "portal_url"}]}
    out = render("demo", doc, {"x": "it"}, closing="", links={"portal_url": "https://p.example/a"})
    assert out.text == "Open it: https://p.example/a"
    bad = render("demo", doc, {"x": "it"}, closing="", links={"portal_url": "javascript:x"})
    assert bad.text == "" and bad.blocks == []


# --- Validation ------------------------------------------------------------

def test_clean_rejects_types_fields_and_sizes():
    _, errs = clean("invoice", {"subject": "S", "blocks": [{"type": "readings"}]})
    assert errs and "isn't available" in errs[0]
    _, errs = clean("invoice", {"subject": "S", "blocks": [{"type": "script", "text": "x"}]})
    assert errs
    _, errs = clean("invoice", {"subject": "S", "blocks": [{"type": "divider"}] * 41})
    assert any("Up to 40" in e for e in errs)
    _, errs = clean("invoice", {"subject": "S", "blocks": [
        {"type": "text", "text": "x" * (email_templates.TEXT_MAX + 1)}]})
    assert any("too long" in e for e in errs)
    doc, errs = clean("invoice", {"subject": "S", "blocks": [
        {"type": "details", "rows": [["Tech", "technician"], ["Total", "total"]]}]})
    assert errs and doc["blocks"][0]["rows"] == [["Total", "total"]]
    _, errs = clean("invoice", {"subject": "  ", "blocks": []})
    assert any("subject" in e for e in errs)
    assert clean("invoice", "nope")[1]


def test_clean_normalises_choices_and_control_characters():
    doc, errs = clean("reminder", {"subject": "S\r\nT", "blocks": [
        {"type": "text", "show": "everywhere", "align": "diagonal", "text": "a\x00b\r\nc"}]})
    assert errs == []
    assert doc["subject"] == "S T"
    assert doc["blocks"][0] == {"type": "text", "show": "both", "text": "ab\nc", "align": "left"}


def test_invalid_saved_json_falls_back_to_default(engine):
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "email_tpl_visit", "{not json")
        models.set_setting(s, "email_tpl_invoice", json.dumps({"subject": "", "blocks": []}))
        assert email_templates.doc(s, "visit") == default_doc("visit")
        assert email_templates.doc(s, "invoice") == default_doc("invoice")


def test_style_hex_is_validated_and_buttons_stay_readable():
    style, errs = email_templates.clean_style({"accent": "red;x", "button": "blob"})
    assert errs and style == {"accent": "", "button": "rounded"}
    style, errs = email_templates.clean_style({"accent": "ffd166", "button": "pill"})
    assert errs == [] and style == {"accent": "#FFD166", "button": "pill"}
    from app import palettes
    pale = email_templates.button_colour("#FFD166")
    assert palettes.ratio(pale, "#FFFFFF") >= 4.5
    assert email_templates.button_colour("#0E2A3B") == "#0E2A3B"


def test_custom_accent_reaches_the_email(engine):
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "email_style", json.dumps({"accent": "#B03A2E", "button": "square"}))
        c = email_brand.colours(s, False)
        assert c["accent"] != email_brand.LAGOON["accent"]
        assert email_brand.button_radius(s) == "2px"


def test_store_image_checks_and_reencodes(data_dir):
    assert email_templates.store_image(b"GIF89a", "image/gif")[1]
    assert email_templates.store_image(b"x" * (2 * 1024 * 1024 + 1), "image/png")[1]
    assert email_templates.store_image(b"not an image", "image/png")[1]
    image_id, err = email_templates.store_image(_png(2400, 600) + b"TRAILING", "image/png")
    assert err is None and len(image_id) == 16
    info = email_templates.image_info(image_id)
    assert info["subtype"] == "png" and info["w"] == 544
    assert Image.open(io.BytesIO(info["data"])).size == (1200, 300)
    assert b"TRAILING" not in info["data"]
    buf = io.BytesIO()
    Image.new("RGB", (30, 30)).save(buf, "JPEG")
    jid, err = email_templates.store_image(buf.getvalue(), "image/jpeg")
    assert err is None and email_templates.image_info(jid)["subtype"] == "jpeg"
    assert email_templates.image_info("../../etc/passwd") is None


def test_clean_rejects_missing_images(data_dir):
    _, errs = clean("visit", {"subject": "S", "blocks": [
        {"type": "image", "image": "0123456789abcdef", "alt": ""}]})
    assert errs


# --- Mailer: HTML + text parts, CID images ----------------------------------

def test_image_block_travels_as_a_cid_part(smtp, data_dir):
    image_id, _ = email_templates.store_image(_png(), "image/png")
    doc = default_doc("invoice")
    doc["blocks"].insert(0, {"type": "image", "show": "both", "image": image_id, "alt": "Our van"})
    m = _invoice(doc=doc)
    assert m.text.startswith("[Our van]\n\nHello Ada Rivera,")
    html = _html(m)
    assert f'src="cid:opp-img-{image_id}"' in html and "data:" not in html
    mailer.send_message("ada@example.com", m.subject, m.text, html=html)
    msg = smtp[0]
    types = [p.get_content_type() for p in msg.walk()]
    assert types[:4] == ["multipart/alternative", "text/plain", "multipart/related", "text/html"]
    img = next(p for p in msg.walk() if p.get_content_type() == "image/png")
    assert img["Content-ID"] == f"<opp-img-{image_id}>"
    assert next(p for p in msg.walk() if p.get_content_type() == "text/plain").get_content().strip() \
        == m.text.strip()


def test_deleted_image_is_dropped_at_send_time(smtp, data_dir):
    image_id, _ = email_templates.store_image(_png(), "image/png")
    doc = default_doc("reminder")
    doc["blocks"].insert(1, {"type": "image", "show": "html", "image": image_id, "alt": ""})
    html = _html(_reminder(doc=doc))
    import os
    os.remove(email_templates.image_file(image_id))
    mailer.send_message("ada@example.com", "S", "T", html=html)
    sent_html = next(p for p in smtp[0].walk() if p.get_content_type() == "text/html").get_content()
    assert "opp-img" not in sent_html
    assert not any(p.get_content_type().startswith("image/") for p in smtp[0].walk())


def test_preview_inlines_images_as_data_uris(engine, data_dir):
    models.Base.metadata.create_all(engine)
    image_id, _ = email_templates.store_image(_png(), "image/png")
    doc = default_doc("visit")
    doc["blocks"].insert(1, {"type": "image", "show": "both", "image": image_id, "alt": ""})
    with Session(engine) as s:
        page = email_brand.preview(s, "visit", email_brand.look(s), doc=doc)
    assert "data:image/png;base64," in page and "cid:" not in page


# --- Routes ------------------------------------------------------------------

def test_builder_is_admin_only(client, tech_client):
    tech = tech_client(name="sam")
    assert tech.get("/settings/emails/visit", follow_redirects=False).status_code == 403
    assert tech.post("/settings/emails/visit", data=form_for(default_doc("visit")),
                     follow_redirects=False).status_code == 403
    assert tech.post("/settings/emails/visit/reset", follow_redirects=False).status_code == 403
    assert tech.get("/settings/emails/visit/preview", follow_redirects=False).status_code == 403


def test_builder_page_renders_each_template(client):
    r = client.get("/settings/emails", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/emails/visit"
    for key in email_templates.TEMPLATES:
        r = client.get(f"/settings/emails/{key}")
        assert r.status_code == 200
        assert 'sandbox=""' in r.text and "srcdoc=" in r.text
        assert 'name="op" value="save"' in r.text
    assert client.get("/settings/emails/nope").status_code == 404
    assert "/settings/emails/visit" in client.get("/settings").text


def test_save_changes_the_sent_email(client, engine):
    doc = default_doc("reminder")
    doc["subject"] = "Friendly nudge: {invoice_number}"
    doc["blocks"][1]["text"] = "Hi there {customer_name}!"
    r = client.post("/settings/emails/reminder", data=form_for(doc, op="save"),
                    follow_redirects=False)
    assert r.status_code == 303 and "done=saved" in r.headers["location"]
    assert _saved(engine, "reminder")["subject"] == "Friendly nudge: {invoice_number}"
    assert "Edited" in client.get("/settings/emails/reminder").text

    from tests.test_ar_routes import _billable_month
    iid, number, _ = _billable_month(engine, client)
    with Session(engine) as s:
        models.set_setting(s, "smtp_host", "smtp.example.com")
        s.get(models.Invoice, iid).due_date = date(2020, 1, 1)
        s.commit()
    client.post(f"/invoices/{iid}/reminder", follow_redirects=False)
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
    assert row.subject == f"Friendly nudge: {number}"
    assert row.body.startswith("Hi there Acme!\n\n")
    assert "Hi there Acme!" in row.html


def test_save_with_errors_keeps_the_draft(client, engine):
    doc = default_doc("invoice")
    doc["blocks"][1]["text"] = "x" * (email_templates.TEXT_MAX + 5)
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="save"))
    assert r.status_code == 400 and "too long" in r.text and "x" * 50 in r.text
    assert _saved(engine, "invoice") is None


def test_unknown_field_is_flagged_but_saves(client, engine):
    doc = default_doc("invoice")
    doc["blocks"][1]["text"] = "Hello {first_name},"
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="refresh"))
    assert r.status_code == 200 and "Unknown field" in r.text and "{first_name}" in r.text
    client.post("/settings/emails/invoice", data=form_for(doc, op="save"))
    assert _saved(engine, "invoice")["blocks"][1]["text"] == "Hello {first_name},"


def test_reorder_buttons_and_order_field(client, engine):
    doc = default_doc("followup")
    # "up" on block 2 swaps it with block 1, and only redraws the draft.
    r = client.post("/settings/emails/followup", data=form_for(doc, op="up:2"))
    assert r.status_code == 200 and "Unsaved changes" in r.text
    assert r.text.index("Our records show") < r.text.index("Hello {customer_name},")
    assert _saved(engine, "followup") is None
    # The drag-to-reorder order field: last block first, then save.
    n = len(doc["blocks"])
    order = ",".join(str(i) for i in [n - 1, *range(n - 1)])
    client.post("/settings/emails/followup", data=form_for(doc, order=order, op="save"))
    saved = _saved(engine, "followup")
    assert saved["blocks"][0]["type"] == "signoff"
    assert [b["type"] for b in saved["blocks"][1:]] == [b["type"] for b in doc["blocks"][:-1]]


def test_add_delete_and_rows_ops(client):
    doc = default_doc("invoice")
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="add:divider"))
    assert r.text.count('value="divider"') == 1
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="add:readings"))
    assert 'value="readings"' not in r.text  # not a block this email offers
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="del:1"))
    assert "Hello {customer_name}," not in r.text
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="rowdel:4.0"))
    assert 'name="b4_rl" value="Invoice"' not in r.text
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="rowadd:4"))
    assert r.text.count('name="b4_rl"') == 5


def test_reset_to_default(client, engine):
    doc = default_doc("visit")
    doc["subject"] = "Done at {property_label}"
    client.post("/settings/emails/visit", data=form_for(doc, op="save"))
    assert _saved(engine, "visit")
    r = client.post("/settings/emails/visit/reset", follow_redirects=False)
    assert r.status_code == 303
    assert _saved(engine, "visit") is None
    assert "Reset to default" not in client.get("/settings/emails/visit").text


def test_preview_routes_show_draft_and_both_parts(client):
    doc = default_doc("invoice")
    doc["blocks"][1]["text"] = "Howdy {customer_name} <3"
    r = client.post("/settings/emails/invoice/preview", data=form_for(doc))
    assert r.status_code == 200 and "Howdy Helen Whitfield &lt;3" in r.text
    r = client.post("/settings/emails/invoice/preview", data=form_for(doc, part="text"))
    assert "<pre" in r.text and "Howdy Helen Whitfield &lt;3" in r.text
    r = client.get("/settings/emails/invoice/preview?part=text")
    assert "Hello Helen Whitfield," in r.text and r.headers["cache-control"] == "no-store"
    # The page's own preview is the draft too.
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="part:text"))
    assert "Howdy Helen Whitfield &amp;lt;3" in r.text  # escaped inside srcdoc


def test_style_saves_with_the_template(client, engine):
    doc = default_doc("visit")
    client.post("/settings/emails/visit", data=form_for(doc, op="save", es_mode="custom",
                                                       es_accent="#b03a2e", es_button="pill"))
    with Session(engine) as s:
        assert json.loads(models.get_setting(s, "email_style", "")) == {
            "accent": "#B03A2E", "button": "pill"}
    r = client.post("/settings/emails/visit", data=form_for(doc, op="save", es_mode="custom",
                                                           es_accent="url(x)"))
    assert r.status_code == 400 and "accent" in r.text


def test_image_upload_through_the_form(client, engine, smtp):
    doc = default_doc("invoice")
    doc["blocks"].insert(0, {"type": "image", "show": "both", "image": "", "alt": "Banner"})
    data = form_for(doc, op="save")
    r = client.post("/settings/emails/invoice", data=data,
                    files={"b0_file": ("banner.png", _png(), "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    image_id = _saved(engine, "invoice")["blocks"][0]["image"]
    assert email_templates.image_file(image_id)
    r = client.post("/settings/emails/invoice", data=form_for(doc, op="refresh"),
                    files={"b0_file": ("x.gif", b"GIF89a", "image/gif")})
    assert "PNG or JPEG" in r.text


def test_send_test_to_the_company_email(client, engine, smtp):
    r = client.post("/settings/emails/visit", data=form_for(default_doc("visit"), op="test"))
    assert "company email" in r.text and not smtp
    with Session(engine) as s:
        models.set_setting(s, "company_email", "owner@example.com")
        s.commit()
    doc = default_doc("visit")
    doc["subject"] = "Draft subject {visit_date}"
    r = client.post("/settings/emails/visit", data=form_for(doc, op="test"))
    assert "Test sent to owner@example.com" in r.text
    assert smtp[0]["To"] == "owner@example.com"
    assert smtp[0]["Subject"] == "[Test] Draft subject October 23, 2026"
    assert _saved(engine, "visit") is None  # a test never saves

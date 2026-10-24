"""Branded HTML emails: one template, plain-text copy always kept, palette
colours applied, everything escaped, and the "What customers see" settings."""
import email
import io
import smtplib
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, email_brand, mailer, models, palettes


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


def _smtp(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_FROM", "office@example.com")
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP.sent


def _types(msg):
    return [p.get_content_type() for p in msg.walk()]


def _part(msg, ctype):
    return next(p for p in msg.walk() if p.get_content_type() == ctype)


def _set(engine, **settings):
    with Session(engine) as s:
        for k, v in settings.items():
            models.set_setting(s, k, v)
        s.commit()


def _sample(**kw):
    base = dict(company_name="Bluewater Pools", customer_name="Ada Rivera", number="1080",
                period="September 2026", total_cents=56000, due=date(2026, 10, 31))
    return email_brand.invoice_mail(**{**base, **kw})


# --- Message structure ------------------------------------------------------

def test_send_message_has_plain_and_html_alternatives(monkeypatch):
    sent = _smtp(monkeypatch)
    mailer.send_message("dest@example.com", "Hi", "Plain body", html="<p>HTML body</p>")
    msg = sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    # Plain text first (the fallback), HTML last (the preferred rendering).
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().strip() == "Plain body"
    assert "HTML body" in parts[1].get_content()


def test_html_with_pdf_attachment_keeps_both_bodies(monkeypatch):
    sent = _smtp(monkeypatch)
    mailer.send_message("d@example.com", "Inv", "text", html="<p>x</p>",
                        attachments=[(b"%PDF-1.4", "1080.pdf")])
    msg = sent[0]
    assert msg.get_content_type() == "multipart/mixed"
    types = _types(msg)
    assert "text/plain" in types and "text/html" in types and "application/pdf" in types


def test_logo_rides_along_as_a_cid_related_part(monkeypatch, tmp_path):
    from PIL import Image

    from app import company

    logo = tmp_path / "logo.png"
    Image.new("RGB", (200, 100), "#123456").save(logo)
    monkeypatch.setattr(company, "logo_path", lambda: str(logo))
    sent = _smtp(monkeypatch)
    mail = _sample()
    html = email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Bluewater Pools", "phone": ""},
                              logo_src=f"cid:{email_brand.LOGO_CID}")
    mailer.send_message("d@example.com", mail.subject, mail.text, html=html)
    msg = sent[0]
    assert "multipart/related" in _types(msg)
    img = _part(msg, "image/png")
    assert img["Content-ID"] == f"<{email_brand.LOGO_CID}>"
    assert Image.open(io.BytesIO(img.get_payload(decode=True))).height <= 64
    assert "text/plain" in _types(msg)


def test_logo_removed_after_queueing_is_dropped_not_broken(monkeypatch, tmp_path):
    from app import company

    monkeypatch.setattr(company, "logo_path", lambda: str(tmp_path / "missing.png"))
    sent = _smtp(monkeypatch)
    html = '<p>a</p><!--logo--><img src="cid:opp-logo"><!--/logo--><p>b</p>'
    mailer.send_message("d@example.com", "s", "t", html=html)
    body = _part(sent[0], "text/html").get_content()
    assert "cid:" not in body and "<p>a</p>" in body and "<p>b</p>" in body
    assert "image/png" not in _types(sent[0])


def test_branded_html_is_email_safe():
    html = email_brand.render(_sample(), colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Bluewater Pools", "phone": "(555) 010-0000"})
    low = html.lower()
    assert "<table" in low and 'role="presentation"' in low
    assert "<script" not in low and "<style" not in low and "<link" not in low
    # No remote images: the only image source allowed is the embedded logo.
    assert "http://" not in low.replace("http://www.w3.org", "")
    assert 'src="http' not in low


# --- Escaping -----------------------------------------------------------------

def test_customer_and_company_text_is_escaped():
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    mail = _sample(customer_name=evil, company_name=evil, signoff=f"Thanks\n{evil}")
    html = email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": evil, "phone": evil})
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The plain-text part is the text itself, unescaped.
    assert evil in mail.text


def test_only_http_links_become_anchors():
    mail = email_brand.sign_request_mail(
        subject="Sign", sign_url="javascript:alert(1)",
        body='Sign here: https://pools.example.com/sign/abc?x=1&y="2"\n\njavascript:alert(1)')
    html = email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Co", "phone": ""})
    assert 'href="https://pools.example.com/sign/abc?x=1&amp;y=' in html
    assert 'href="javascript' not in html
    assert "Review and sign" not in html  # the unsafe CTA was dropped


# --- Settings applied -----------------------------------------------------------

def test_company_palette_colours_the_email(engine):
    from app.db import Base

    Base.metadata.create_all(engine)
    _set(engine, palette="coral", email_brand="1")
    want = palettes.derive(palettes.BY_KEY["coral"].base, "light")
    with Session(engine) as s:
        html = email_brand.html_for(s, _sample())
    assert want["--mast-a"] in html and want["--as"] in html
    assert email_brand.LAGOON["mast_a"] not in html

    _set(engine, email_brand="0")  # off: the standard Lagoon teal
    with Session(engine) as s:
        html = email_brand.html_for(s, _sample())
    assert want["--mast-a"] not in html and email_brand.LAGOON["mast_a"] in html


def test_custom_palette_hex_is_validated(engine):
    from app.db import Base

    Base.metadata.create_all(engine)
    _set(engine, palette="custom", palette_custom="red;background:url(x)")
    with Session(engine) as s:
        c = email_brand.colours(s, True)
    assert c["mast_a"] == email_brand.LAGOON["mast_a"]
    assert all(palettes.clean_hex(v) == v for v in c.values())


def test_plain_only_setting_sends_no_html(client, engine, monkeypatch):
    sent = _smtp(monkeypatch)
    _set(engine, email_format="plain")
    client.post("/settings/mail/test", data={"to_addr": "dest@example.com"})
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.html == "" and row.status == "sent"
    msg = sent[0]
    assert msg.get_content_type() == "text/plain"
    assert "text/html" not in _types(msg)


def test_outbox_stores_and_delivers_the_html(client, engine, monkeypatch):
    sent = _smtp(monkeypatch)
    client.post("/settings/mail/test", data={"to_addr": "dest@example.com"})
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
        assert row.status == "sent" and "<table" in row.html
    types = _types(sent[0])
    assert types[:3] == ["multipart/alternative", "text/plain", "text/html"]


def test_failed_branded_row_retries_with_its_html(engine, monkeypatch):
    from app.db import Base

    Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    calls = []

    def flaky(to, subject, body, conf=None, attachments=None, html=None):
        calls.append(html)
        if len(calls) == 1:
            raise RuntimeError("busy")

    monkeypatch.setattr(mailer, "send_message", flaky)
    with Session(engine) as s:
        mailer.queue(s, "a@example.com", "s", "plain", html="<p>branded</p>")
        s.commit()
        assert mailer.flush_outbox(s) == (0, 1)
        assert mailer.flush_outbox(s) == (1, 0)
    assert calls == ["<p>branded</p>", "<p>branded</p>"]


def test_signoff_closes_app_written_emails_in_both_parts():
    mail = _sample(signoff="Thanks for trusting us with your pool.\nThe Bluewater team")
    assert mail.text.endswith("Thanks for trusting us with your pool.\nThe Bluewater team")
    html = email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Bluewater Pools", "phone": ""})
    assert "Thanks for trusting us with your pool.<br>The Bluewater team" in html
    # Blank sign-off: the body ends with the company name, as it always did.
    assert _sample().text.endswith("Thank you for your business.\n\nBluewater Pools")


def test_visit_email_is_branded_with_plain_copy(client, engine):
    with Session(engine) as s:
        c = models.Customer(name="V", email="cust@example.com", notify_email=True)
        p = models.Property(customer=c, label="Home <b>", frequency="weekly", weekday=0)
        s.add(p)
        s.add(models.Pool(property=p, gallons=12000))
        s.commit()
        pid = p.id
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).first().id
    client.post(f"/visit/{vid}/complete", data={"fc": "3", "ph": "7.4", "ta": "", "cya": "",
                                                "salt": "", "tasks": ["Brush walls"], "notes": ""})
    with Session(engine) as s:
        row = s.scalars(select(models.EmailOutbox)).one()
    assert "Home <b> — July 13, 2026" in row.body
    assert "Home &lt;b&gt;" in row.html and "Home <b>" not in row.html
    assert "Your service visit is complete" in row.html and "Brush walls" in row.html


# --- Settings card and route -------------------------------------------------------

def test_settings_card_sits_after_appearance(client):
    html = client.get("/settings").text
    a, b = html.index(">Appearance</h2>"), html.index(">What customers see</h2>")
    assert a < b < html.index(">Backups</h2>")
    assert 'action="/settings/customer-look"' in html
    assert 'sandbox=""' in html and "/settings/customer-look/preview/visit" in html


def test_customer_look_saves_valid_values(client, engine):
    r = client.post("/settings/customer-look", data={
        "brand": ["0"], "logo": ["0", "1"], "format": "plain", "signoff": "  Thanks!\r\nThe team  "},
        follow_redirects=False)
    assert r.status_code == 303 and "look=saved" in r.headers["location"]
    with Session(engine) as s:
        assert email_brand.look(s) == {"brand": False, "logo": True, "format": "plain",
                                       "signoff": "Thanks!\nThe team"}


def test_customer_look_ignores_unknown_values(client, engine):
    _set(engine, email_brand="1", email_logo="0", email_format="html", email_signoff="Cheers")
    r = client.post("/settings/customer-look", data={
        "brand": "yes", "logo": "2", "format": "fancy", "signoff": "x" * 301},
        follow_redirects=False)
    assert "look=error" in r.headers["location"]
    with Session(engine) as s:
        assert email_brand.look(s) == {"brand": True, "logo": False, "format": "html",
                                       "signoff": "Cheers"}
    # A form missing a field entirely keeps that field too.
    client.post("/settings/customer-look", data={"format": "plain"})
    with Session(engine) as s:
        lk = email_brand.look(s)
        assert lk["format"] == "plain" and lk["brand"] is True and lk["signoff"] == "Cheers"


def test_customer_look_is_admin_only(tech_client):
    tc = tech_client()
    r = tc.post("/settings/customer-look", data={"format": "plain"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert tc.get("/settings/customer-look/preview/visit",
                  follow_redirects=False).status_code in (303, 403)


def test_previews_render_all_four_emails(client, engine):
    _set(engine, business_name="Bluewater <Pools>")
    for kind, heading in (("visit", "Your service visit is complete"),
                          ("invoice", "Your September 2026 invoice"),
                          ("reminder", "A reminder about invoice 1049"),
                          ("sign", "Your agreement is ready to sign")):
        r = client.get(f"/settings/customer-look/preview/{kind}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert heading in r.text and "Bluewater &lt;Pools&gt;" in r.text
        assert "<script" not in r.text.lower()
        assert "script-src" in r.headers["content-security-policy"]
    assert client.get("/settings/customer-look/preview/nope").status_code == 404


def test_preview_follows_unsaved_choices(client):
    plain = client.get("/settings/customer-look/preview/invoice",
                       params={"format": "plain", "signoff": "Ta <3"})
    assert "<pre" in plain.text and "Ta &lt;3" in plain.text and "<table" not in plain.text
    off = client.get("/settings/customer-look/preview/invoice", params={"brand": "0"})
    assert email_brand.LAGOON["mast_a"] in off.text
    bad = client.get("/settings/customer-look/preview/invoice", params={"format": "<x>"})
    assert "<table" in bad.text  # invalid override falls back to the saved format


def test_parsed_message_round_trips(monkeypatch):
    """The generated MIME parses back cleanly (what a mail client sees)."""
    sent = _smtp(monkeypatch)
    mail = _sample()
    html = email_brand.render(mail, colours={**email_brand.NEUTRAL, **email_brand.LAGOON},
                              company={"name": "Bluewater Pools", "phone": ""})
    mailer.send_message("d@example.com", mail.subject, mail.text, html=html)
    parsed = email.message_from_bytes(sent[0].as_bytes(), policy=email.policy.default)
    assert parsed.get_body(("plain",)).get_content().strip() == mail.text
    assert "Your September 2026 invoice" in parsed.get_body(("html",)).get_content()

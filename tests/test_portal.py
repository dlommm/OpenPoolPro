"""The customer portal: the only surface reachable without a staff login.

Everything here is about what a bearer token is and is not allowed to see.
The portal had no tests at all, which for a token-authenticated, unauthenticated
surface is the coverage gap that matters most: a resolve() that accepted a
short token, an expired row, or a revoked one would have shipped green.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, portal

HTTPS_BASE = "https://pool.example.test"


def _fixtures(engine):
    """Two customers, each with a property, a done visit, a sent invoice, a
    signed contract and a photo. Only the first customer's photo is shared.
    """
    with Session(engine) as s:
        models.set_setting(s, "public_base_url", HTTPS_BASE)
        ours = models.Customer(name="Alvarez Household", email="a@example.test", active=True)
        theirs = models.Customer(name="Private Neighbour", email="n@example.test", active=True)
        s.add_all([ours, theirs])
        s.flush()
        ids = {}
        for customer, tag in ((ours, "ours"), (theirs, "theirs")):
            prop = models.Property(customer_id=customer.id, label="Home",
                                   address=f"{tag} street", street=f"{tag} street",
                                   city="Cocoa", state="FL", zip="32922",
                                   frequency="weekly", weekday=1, anchor_date=date.today())
            s.add(prop)
            s.flush()
            pool = models.Pool(property_id=prop.id, label="Pool", shape="rectangle", kind="pool",
                               length_ft=30, width_ft=15, avg_depth_ft=5, gallons=16000,
                               water_type="chlorine", surface="plaster", active=True)
            visit = models.Visit(property_id=prop.id, date=date.today() - timedelta(days=3),
                                 status="done", resolved=True, fc=3.2, ph=7.5,
                                 tasks_done=[f"Brush {tag} walls"], completed_at=models.utcnow())
            invoice = models.Invoice(number=f"INV-{tag}", customer_id=customer.id,
                                    period_year=2026, period_month=8, total_cents=12000,
                                    tax_cents=0, status="sent", due_date=date(2026, 9, 1),
                                    pdf_filename=f"{tag}.pdf")
            photo = models.Photo(entity_type="property", entity_id=prop.id,
                                 filename=f"{tag}.jpg", thumb_filename=f"{tag}-t.jpg",
                                 caption=f"{tag} photo", tag="after")
            s.add_all([pool, visit, invoice, photo])
            s.flush()
            s.add(models.Contract(pool_id=pool.id, season_year=2026, weeks=30, status="signed",
                                  weekly_cents=9000, pdf_filename=f"{tag}-contract.pdf"))
            ids[tag] = {"customer": customer.id, "property": prop.id,
                        "invoice": invoice.id, "photo": photo.id}
        # Sharing is per photo and opt-in: the neighbour's photo stays private
        # even from the neighbour's own portal until someone shares it.
        s.add(models.PortalPhoto(photo_id=ids["ours"]["photo"]))
        s.commit()
        return ids


@pytest.fixture
def anon(client):
    """A client carrying no session at all -- how a customer arrives. Built
    from the same app so it shares the engine and the in-memory database."""
    return TestClient(client.app)


def _token(engine, customer_id, days=30):
    with Session(engine) as s:
        token = portal.issue(s, customer_id, days)
        s.commit()
    return token


def test_issue_requires_an_https_public_base(client, engine):
    """Without a public base URL there is nothing to build a link out of, and
    the operator gets a readable page saying so -- not a raw JSON 400."""
    ids = _fixtures(engine)
    with Session(engine) as s:
        models.set_setting(s, "public_base_url", "http://insecure.example.test")
        s.commit()
    # Accept: text/html is what makes this a browser rather than an API caller,
    # and it is the whole difference between a readable page and a JSON blob
    # filling the window.
    r = client.post(f"/customers/{ids['ours']['customer']}/portal/issue",
                    data={"days": 30}, headers={"accept": "text/html"})
    assert r.status_code == 400
    assert "HTTPS Public base URL" in r.text
    assert "<html" in r.text.lower()
    # An API or htmx caller still gets the structured default.
    api = client.post(f"/customers/{ids['ours']['customer']}/portal/issue",
                      data={"days": 30}, headers={"accept": "application/json"})
    assert api.status_code == 400 and api.json()["detail"]


def test_issue_returns_a_link_the_customer_can_open(client, engine, anon):
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]
    r = client.post(f"/customers/{cid}/portal/issue", data={"days": 30})
    assert r.status_code == 200
    assert HTTPS_BASE + "/portal/" in r.text
    token = r.text.split(HTTPS_BASE + "/portal/")[1].split('"')[0]

    # A context with no session at all -- how a customer arrives.
    page = anon.get(f"/portal/{token}")
    assert page.status_code == 200
    assert "Alvarez Household" in page.text
    assert "Brush ours walls" in page.text
    # Only the token is authority: no staff shell, no way into the app.
    assert 'id="topbar"' not in page.text
    assert 'href="/money"' not in page.text


def test_portal_shows_nothing_belonging_to_another_customer(client, engine, anon):
    ids = _fixtures(engine)
    page = anon.get(f"/portal/{_token(engine, ids['ours']['customer'])}")
    body = page.text
    assert "INV-ours" in body
    for leaked in ("Private Neighbour", "INV-theirs", "Brush theirs walls", "theirs street"):
        assert leaked not in body, f"portal leaked {leaked!r}"
    # Nor by asking for the neighbour's documents directly.
    for path in (f"/portal/{_token(engine, ids['ours']['customer'])}/invoices/{ids['theirs']['invoice']}",
                 f"/portal/{_token(engine, ids['ours']['customer'])}/photos/{ids['theirs']['photo']}"):
        assert anon.get(path).status_code == 404


def test_only_shared_photos_reach_the_portal(client, engine, anon):
    """Sharing is opt-in per photo: an eligible but unshared photo is invisible
    on the customer's own portal."""
    ids = _fixtures(engine)
    shared = anon.get(f"/portal/{_token(engine, ids['ours']['customer'])}")
    assert f"/photos/{ids['ours']['photo']}" in shared.text

    unshared = anon.get(f"/portal/{_token(engine, ids['theirs']['customer'])}")
    assert "No photos shared yet" in unshared.text
    assert anon.get(
        f"/portal/{_token(engine, ids['theirs']['customer'])}/photos/{ids['theirs']['photo']}"
    ).status_code == 404


@pytest.mark.parametrize("bad", ["short", "z" * 39, "z" * 129])
def test_a_token_of_the_wrong_length_is_refused_outright(client, engine, anon, bad):
    _fixtures(engine)
    r = anon.get(f"/portal/{bad}")
    assert r.status_code == 404
    assert "unavailable" in r.text


def test_expired_revoked_and_deactivated_access_all_close_the_door(client, engine, anon):
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]

    expired = _token(engine, cid)
    with Session(engine) as s:
        s.get(models.PortalAccess, cid).expires_at = models.utcnow() - timedelta(minutes=1)
        s.commit()
    assert anon.get(f"/portal/{expired}").status_code == 404

    live = _token(engine, cid)
    assert anon.get(f"/portal/{live}").status_code == 200
    client.post(f"/customers/{cid}/portal/revoke")
    assert anon.get(f"/portal/{live}").status_code == 404

    reissued = _token(engine, cid)
    with Session(engine) as s:
        s.get(models.Customer, cid).active = False
        s.commit()
    assert anon.get(f"/portal/{reissued}").status_code == 404


def test_reissuing_replaces_the_previous_link(client, engine, anon):
    """One live link per customer: the page says the old one stops working, so
    it has to actually stop working."""
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]
    first = _token(engine, cid)
    second = _token(engine, cid)
    assert anon.get(f"/portal/{first}").status_code == 404
    assert anon.get(f"/portal/{second}").status_code == 200


def test_issuing_for_an_inactive_customer_is_refused(client, engine):
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]
    with Session(engine) as s:
        s.get(models.Customer, cid).active = False
        s.commit()
    r = client.post(f"/customers/{cid}/portal/issue", data={"days": 30})
    assert r.status_code == 409
    assert "Reactivate" in r.text


def test_a_tech_cannot_issue_or_revoke_portal_access(tech_client, engine):
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]
    tech = tech_client()
    assert tech.get(f"/customers/{cid}/portal").status_code == 403
    assert tech.post(f"/customers/{cid}/portal/issue", data={"days": 30}).status_code == 403
    assert tech.post(f"/customers/{cid}/portal/revoke").status_code == 403


def test_the_portal_wears_the_company_theme(client, engine, anon):
    """The portal is this company's storefront. A company on control-room was
    emailing links that opened in warm-coastal -- a skin the customer had never
    seen -- because the theme was only resolved for authenticated requests.
    """
    ids = _fixtures(engine)
    with Session(engine) as s:
        models.set_setting(s, "active_theme", "control-room")
        s.commit()
    assert 'data-theme="control-room"' in anon.get(
        f"/portal/{_token(engine, ids['ours']['customer'])}").text
    # The dead-link page too: it renders from the exception handler, which sits
    # behind the same middleware.
    assert 'data-theme="control-room"' in anon.get(f"/portal/{'z' * 48}").text


def test_an_unknown_theme_setting_does_not_reach_the_portal_markup(client, engine, anon):
    ids = _fixtures(engine)
    with Session(engine) as s:
        models.set_setting(s, "active_theme", "bogus-theme")
        s.commit()
    assert 'data-theme="warm-coastal"' in anon.get(
        f"/portal/{_token(engine, ids['ours']['customer'])}").text


def test_draft_paperwork_stays_staff_internal(client, engine, anon):
    """An invoice or agreement is staff-internal until it is sent: a customer
    should never read a figure that is still being decided."""
    ids = _fixtures(engine)
    cid = ids["ours"]["customer"]
    with Session(engine) as s:
        s.get(models.Invoice, ids["ours"]["invoice"]).status = "draft"
        for contract in s.scalars(select(models.Contract)).all():
            contract.status = "draft"
        s.commit()
    token = _token(engine, cid)
    body = anon.get(f"/portal/{token}").text
    assert "INV-ours" not in body
    assert "No invoices to show yet" in body
    assert "No agreements to show yet" in body
    assert anon.get(f"/portal/{token}/invoices/{ids['ours']['invoice']}").status_code == 404

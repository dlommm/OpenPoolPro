"""Billing master/detail: the selected draft's review beside the list.

The pane is a convenience over the full review page, never a second set of
rules: it must carry the same permissions, the same actions, and fall back to
plain pages whenever HTMX is not the one asking."""

from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

from app import models
from tests.test_qbo_billing_routes import _records, _reserve

HX = {"HX-Request": "true"}


def _query(response):
    parts = urlsplit(response.headers["location"])
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_pane_returns_the_review_fragment_for_htmx(client, engine):
    _, _, did, *_ = _records(engine)
    r = client.get(f"/billing/drafts/{did}/pane", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "Billing Customer" in r.text
    assert f'action="/billing/drafts/{did}/edit"' in r.text
    assert 'name="back" value="list"' in r.text
    assert "Approve and send for invoicing" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_pane_without_htmx_falls_back_to_the_split_page(client, engine):
    _, _, did, *_ = _records(engine)
    r = client.get(f"/billing/drafts/{did}/pane", follow_redirects=False)
    assert r.status_code == 303
    assert _query(r) == ("/billing", {"ym": "2026-07", "draft": str(did)})


def test_split_page_renders_list_and_selected_review(client, engine):
    _, _, did, *_ = _records(engine)
    # A stale month in the URL is overridden by the open draft's own month.
    page = client.get(f"/billing?ym=2025-01&draft={did}")
    assert page.status_code == 200
    assert "July 2026" in page.text
    assert f'href="/billing/drafts/{did}" data-pane="/billing/drafts/{did}/pane"' in page.text
    assert 'aria-current="true"' in page.text
    assert 'id="qb-pane"' in page.text and 'aria-labelledby="qb-pane-t"' in page.text
    assert f'action="/billing/drafts/{did}/approve"' in page.text
    assert 'name="back" value="list"' in page.text
    assert client.get("/billing?draft=999999").status_code == 404


def test_list_without_selection_has_no_review(client, engine):
    _, _, did, *_ = _records(engine)
    page = client.get("/billing?ym=2026-07")
    assert page.status_code == 200
    assert f'href="/billing/drafts/{did}"' in page.text
    assert "qb-pane-empty" in page.text
    assert 'aria-current="true"' not in page.text.split('id="qb-ws"')[1].split("</section>")[0]
    assert "/edit" not in page.text


def test_full_review_page_still_stands_alone(client, engine):
    _, _, did, *_ = _records(engine)
    page = client.get(f"/billing/drafts/{did}")
    assert page.status_code == 200
    assert "Billing Customer" in page.text
    assert 'name="back"' not in page.text
    assert "QuickBooks billing</a>" in page.text  # breadcrumb back to the list


def test_pane_and_split_view_keep_billing_permissions(tech_client, engine):
    _, _, did, *_ = _records(engine)
    denied = tech_client(name="denied")
    assert denied.get(f"/billing/drafts/{did}/pane", headers=HX).status_code == 403
    assert denied.get(f"/billing?draft={did}").status_code == 403

    viewer = tech_client(name="viewer", view_money=True)
    pane = viewer.get(f"/billing/drafts/{did}/pane", headers=HX)
    assert pane.status_code == 200
    assert "Approve and send for invoicing" not in pane.text
    assert "An owner or billing approver must approve delivery." in pane.text
    split = viewer.get(f"/billing?draft={did}")
    assert "Approve and send for invoicing" not in split.text
    assert "Billing setup" not in split.text  # admin-only drawer stays admin-only
    assert viewer.post(f"/billing/drafts/{did}/approve", data={"revision": 1, "back": "list"}).status_code == 403

    approver = tech_client(name="approver", approve_billing=True)
    assert "Approve and send for invoicing" in approver.get(f"/billing/drafts/{did}/pane", headers=HX).text


def test_pane_actions_return_to_the_split_view(client, engine):
    _, _, did, *_ = _records(engine)
    form = {
        "revision": 1,
        "due_date": "2026-08-15",
        "email": "bill@example.test",
        "description_0": "Service visit",
        "amount_0": "120.00",
        "back": "list",
    }
    r = client.post(f"/billing/drafts/{did}/edit", data=form, follow_redirects=False)
    assert r.status_code == 303
    path, q = _query(r)
    assert (path, q["ym"], q["draft"]) == ("/billing", "2026-07", str(did))
    assert q["message"].startswith("Draft saved")
    with Session(engine) as s:
        assert s.get(models.QBODraft, did).total_cents == 12000

    # Refusals land in the split view too, with the reason shown on the list.
    r = client.post(f"/billing/drafts/{did}/approve", data={"revision": 2, "back": "list"})
    assert r.status_code == 200
    assert "Enable reviewed QuickBooks billing" in r.text
    assert 'aria-current="true"' in r.text


def test_full_page_actions_keep_their_redirect(client, engine):
    _, _, did, *_ = _records(engine)
    form = {
        "revision": 1,
        "due_date": "2026-08-15",
        "email": "bill@example.test",
        "description_0": "Service visit",
        "amount_0": "100.00",
    }
    r = client.post(f"/billing/drafts/{did}/edit", data=form, follow_redirects=False)
    assert _query(r)[0] == f"/billing/drafts/{did}"
    # Only the fixed token switches the target: no open redirect via back=.
    r = client.post(
        f"/billing/drafts/{did}/edit",
        data={**form, "revision": 2, "back": "https://evil.example/"},
        follow_redirects=False,
    )
    assert _query(r)[0] == f"/billing/drafts/{did}"


def test_discard_from_the_pane_returns_to_that_month(client, engine):
    _, _, did, *_ = _records(engine)
    _reserve(engine, did)
    r = client.post(f"/billing/drafts/{did}/discard", data={"revision": 1, "back": "list"}, follow_redirects=False)
    path, q = _query(r)
    assert (path, q["ym"]) == ("/billing", "2026-07")
    assert "draft" not in q
    assert q["message"].startswith("Unsubmitted draft discarded")
    with Session(engine) as s:
        assert s.get(models.QBODraft, did) is None

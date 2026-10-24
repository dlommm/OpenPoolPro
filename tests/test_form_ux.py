"""Form UX safety nets.

A required Form(...) field left blank used to answer a browser's POST with
FastAPI's raw-JSON 422 — unreadable, and it looked like the app ate everything
typed. Browsers now get a readable page with a Go-back button; htmx and
non-HTML callers keep the structured default. Client-side, the mandatory
inputs carry `required` so the round trip rarely happens at all, and every
plain form gets a double-submit guard.
"""
import pathlib
import re

BROWSER_ACCEPT = {"accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}


def test_browser_form_422_gets_a_readable_page(client):
    r = client.post("/customers/new", data={"phone": "555"}, headers=BROWSER_ACCEPT)
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "That didn't save" in r.text or "That didn&#39;t save" in r.text
    assert "name" in r.text          # names the missing field
    assert "Go back" in r.text


def test_non_browser_422_keeps_structured_json(client):
    # httpx's default Accept is */* — API-ish callers keep the machine format.
    r = client.post("/customers/new", data={"phone": "555"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")


def test_htmx_422_keeps_structured_json(client):
    r = client.post("/customers/new", data={"phone": "555"},
                    headers={**BROWSER_ACCEPT, "hx-request": "true"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")


def test_mandatory_inputs_carry_required(client):
    """`required` stops the lost-form-data round trip in the browser itself.
    Server-side Form(...) still enforces; this is the first line."""
    html = client.get("/customers/new").text
    assert re.search(r'<input[^>]*name="name"[^>]*\brequired\b', html, re.S), \
        "customer name must be required client-side (server 422s without it)"
    # Source-level for templates that need seeded data to render:
    rule_edit = pathlib.Path("app/templates/customers/property_form.html").read_text()
    assert re.search(r'name="title" value="\{\{ r\.title \}\}" required', rule_edit)
    users = pathlib.Path("app/templates/users/form.html").read_text()
    assert re.search(r'name="password"[^>]*\brequired\b', users, re.S)


def test_double_submit_guard_ships_on_every_page(client):
    """The guard lives in _offline.html, which every base template includes —
    a double-tapped "Complete visit" must not POST (or queue) twice."""
    html = client.get("/").text
    assert "data-submit-guard" in html
    assert "releaseSubmitGuards" in html
    assert 'addEventListener("pageshow", releaseSubmitGuards)' in html
    src = pathlib.Path("app/templates/_offline.html").read_text()
    # Buttons are disabled AFTER the submit event (button name=value pairs
    # must still serialize) and htmx forms are left to their own lifecycle.
    assert "setTimeout" in src.split("double-submit guard")[1].split("pageshow")[0]
    assert 'form.hasAttribute("hx-post")' in src

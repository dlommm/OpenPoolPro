"""P9 Task 5 offline PWA: server-side pytest cannot execute service-worker
JS, so these tests pin the server-visible contract only -- the sw.js text
markers a client-side/manual check depends on, the static offline fallback
being reachable anonymously, the base.html sync-badge/flush-queue wiring,
and the queued-banner query param on the Today page."""
import app.config as config
from fastapi.testclient import TestClient

from app.main import create_app


def _anon_client(engine, monkeypatch):
    """Same convention as tests/test_auth.py's make_client -- a client that
    never logs in, for exercising the auth-exemption allowlist."""
    monkeypatch.setattr(config, "APP_PASSWORD", "unused-anon")
    return TestClient(create_app(engine=engine))


def test_sw_js_contains_v3_markers(client):
    text = client.get("/sw.js").text
    assert "cpp-static-v3" in text
    assert "cpp-pages-v1" in text
    assert "cpp-queue" in text


def test_sw_js_reachable_anonymously(engine, monkeypatch):
    """/sw.js must be exempt from auth (a logged-out/expired session still
    needs the worker registered so cached pages keep working)."""
    c = _anon_client(engine, monkeypatch)
    assert c.get("/sw.js").status_code == 200


def test_offline_html_serves_200_anon(engine, monkeypatch):
    """/static/offline.html must serve without a session -- it's the
    fallback page shown when there's no cached copy of the requested
    navigation AND no network, so it can never depend on being logged in.
    The static prefix is already auth-exempt (app/main.py EXEMPT_PREFIX);
    this pins that exemption for the specific file sw.js falls back to."""
    c = _anon_client(engine, monkeypatch)
    r = c.get("/static/offline.html")
    assert r.status_code == 200
    assert "You're offline" in r.text
    assert "cached pages still work from the menu" in r.text


def test_base_html_has_sync_badge_and_flush_queue_script(client):
    html = client.get("/").text
    assert 'id="sync-badge"' in html
    assert 'class="tag tag-safety"' in html
    assert "flush-queue" in html
    assert "queue-count" in html


def test_today_queued_banner_present_with_param_absent_without(client):
    banner = "Saved offline — will sync when back online"
    assert banner in client.get("/?queued=1").text
    assert banner not in client.get("/").text
    # Garbage/false-ish values must not 500 or show the banner.
    r = client.get("/?queued=0")
    assert r.status_code == 200
    assert banner not in r.text


def test_today_alias_path_serves_same_page(client):
    """sw.js's queued-POST redirect target is literally `/today?queued=1`;
    `/today` is an alias onto the same today_page handler as `/`."""
    r = client.get("/today?queued=1")
    assert r.status_code == 200
    assert "Saved offline — will sync when back online" in r.text

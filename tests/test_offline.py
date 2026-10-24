"""P9 Task 5 offline PWA: server-side pytest cannot execute service-worker
JS, so these tests pin the server-visible contract only -- the sw.js text
markers a client-side/manual check depends on, the static offline fallback
being reachable anonymously, the base.html sync-badge/flush-queue wiring,
and the queued-banner query param on the Today page."""
from fastapi.testclient import TestClient

import app.config as config
from app.main import create_app


def _anon_client(engine, monkeypatch):
    """Same convention as tests/test_auth.py's make_client -- a client that
    never logs in, for exercising the auth-exemption allowlist."""
    monkeypatch.setattr(config, "APP_PASSWORD", "unused-anon")
    return TestClient(create_app(engine=engine))


def test_sw_js_versioned_cache_names_and_no_cache(client):
    """Cache names now derive from APP_VERSION so sw.js's bytes change on every
    release (the browser then detects a new worker and purges old caches). The
    __APP_VERSION__ token must be substituted at serve time, and /sw.js must be
    served no-cache so the version check isn't itself cached."""
    r = client.get("/sw.js")
    text = r.text
    assert "cpp-static-" in text
    assert "cpp-pages-" in text
    assert "cpp-queue" in text
    assert "__APP_VERSION__" not in text          # token substituted
    assert '"dev"' in text                          # config.APP_VERSION default in tests
    assert r.headers.get("cache-control") == "no-cache"


def test_manifest_and_html_served_no_cache(client):
    """/manifest.json and HTML pages revalidate every load so a home-screen
    PWA can't render a stale shell after a deploy."""
    assert client.get("/manifest.json").headers.get("cache-control") == "no-cache"
    assert "no-cache" in (client.get("/").headers.get("cache-control") or "")


def test_assets_are_version_busted(client):
    html = client.get("/").text
    assert "/static/app.css?v=" in html
    assert "/static/htmx.min.js?v=" in html


def test_pwa_partial_wires_update_and_pull_to_refresh(client):
    """The shared _pwa.html partial (included by every base) must ship the
    auto-update reload path and the pull-to-refresh gesture."""
    html = client.get("/").text
    assert "controllerchange" in html      # apply a shipped update
    assert "reg.update" in html            # check for a new worker
    assert "touchstart" in html            # pull-to-refresh gesture
    assert 'id="pwa-ptr"' in html          # pull/refresh indicator element


def test_sw_js_queued_redirect_targets_root_not_today(client):
    """Critical fix pin: the offline-queue redirect must land on `/`
    (the Today page's real mount), not the dead `/today` path -- landing
    on an uncached URL while still offline fell through to offline.html
    with no banner. Also pins that handleNavigate's offline fallback tries
    an ignoreSearch cache match (so the `?queued=1` navigation still hits
    the cached page) before giving up to offline.html."""
    text = client.get("/sw.js").text
    assert '"/?queued=1"' in text
    assert "/today?queued=1" not in text
    assert "ignoreSearch" in text


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
    assert "mast-sync" in html
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


def test_today_path_not_mounted(client):
    """The Today page has exactly one mount, `/` -- sw.js's queued-POST
    redirect target is `/?queued=1`. `/today` was a review-flagged alias
    that has been removed; pin that it stays gone (a 404, not the page)."""
    r = client.get("/today?queued=1")
    assert r.status_code == 404

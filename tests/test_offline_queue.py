"""Offline queue trustworthiness.

There is no JS test harness in this repo, so the service worker cannot be
executed here. What CAN be pinned meaningfully is the *shape* of the decision
that caused the bug: `replay()` used to `break` out of a FIFO on ANY non-2xx,
so one permanently-dead entry (a 404 because the visit was deleted while the
tech was offline, a 400/409/422 the server will reject forever) sat at the head
of the queue and buried every completion behind it, invisibly, behind a
"3 waiting" badge with no way to look inside.

These tests assert against the sw.js source that:
  * a definitive 4xx is parked in a `failed` store and replay CONTINUES,
  * the redirect-to-login guard survives untouched (an expired session must
    never destroy a tech's queued readings),
  * network errors and 5xx still stop the flush,
  * the VERSION-derived cache names and the activate-time purge still work,
  * what gets queued (urlencoded POSTs to /visit/*/complete|skip) is unchanged.

Plus, through the rendered page: the queue screen and the global htmx error
handlers actually ship, in every theme.
"""
import pathlib
import re

import pytest

from app.main import ALLOWED_THEMES

SW_PATH = pathlib.Path("app/static/sw.js")
OFFLINE_PARTIAL = pathlib.Path("app/templates/_offline.html")
BASES = [pathlib.Path("app/templates/base.html")] + [
    pathlib.Path("app/templates/themes") / t / "base.html" for t in sorted(ALLOWED_THEMES)
]


def sw_source():
    return SW_PATH.read_text()


def js_function_body(src, signature):
    """Return the brace-balanced body of `signature` so a test can reason about
    one function instead of the whole file (several functions contain `break`)."""
    start = src.index(signature)
    open_brace = src.index("{", start)
    depth, i = 0, open_brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {signature!r}")


# --------------------------------------------------------------------------
# replay(): the jam
# --------------------------------------------------------------------------

def test_replay_parks_definitive_4xx_instead_of_breaking():
    """The whole bug in one assertion: the arm that handles a dead 4xx must
    hand the entry to the failed store, and must NOT be a `break` — otherwise
    the entry is still at the head of the FIFO on the next flush."""
    body = js_function_body(sw_source(), "async function replayOnce()")
    assert "failEntry(" in body, "replay() no longer parks definitively-failed entries"
    # `failEntry` is the last arm of the if/else chain; nothing after it may bail
    # out of the loop, or the queue behind a dead entry stays buried.
    tail = body.split("failEntry(", 1)[1]
    assert "break" not in tail, "replay() still breaks out of the loop on a dead 4xx"


def test_replay_still_breaks_on_network_error_and_5xx_and_login():
    """Retryable failures must still stop the flush so enqueue order is kept.
    A network error means we went offline again; a 5xx is a server blip; the
    login redirect means the session expired — none of them are the entry's
    fault and all three can succeed later."""
    body = js_function_body(sw_source(), "async function replayOnce()")
    assert re.search(r"catch \(err\) \{\s*break;", body), \
        "a network error mid-flush must keep this entry and the rest queued"
    assert re.search(r"else if \(loginRedirect \|\| resp\.status >= 500[^)]*\)\s*\{\s*"
                     r"(?:if \(loginRedirect\) BLOCKED_AUTH = true;\s*)?break;", body), \
        "login redirect / 5xx must remain retryable (break), not be parked as failed"


def test_login_redirect_guard_is_intact():
    """An expired session answers the replayed POST with 303 -> /login, which
    fetch follows to a 200 login page. Deleting the entry on that would destroy
    a tech's readings, so success requires BOTH resp.ok and a non-login URL."""
    src = sw_source()
    assert 'finalPath === "/login"' in src
    body = js_function_body(src, "async function replayOnce()")
    assert re.search(r"if \(resp\.ok && !loginRedirect\)\s*\{\s*await queueDelete", body), \
        "the queue entry must only be deleted on a real success, never on the login redirect"


def test_replay_is_single_flight():
    """replay is triggered from four places (activate, the flush-queue message,
    Background Sync, retryFailed); two overlapping runs could each POST the head
    entry before either deletes it — the server would receive the same
    completion twice. Overlapping callers must share the run already in flight."""
    src = sw_source()
    wrapper = js_function_body(src, "function replay()")
    assert "REPLAY_INFLIGHT" in wrapper
    assert "replayOnce()" in wrapper
    # Nothing may call replayOnce() directly and bypass the latch.
    assert src.count("replayOnce()") == 2  # the definition's callsite + this wrapper


def test_replay_reports_auth_blocked_state():
    """A replay stopped by the login redirect keeps every entry queued (the
    readings are safe) — but the queue screen must then say 'log in', not show
    an eternal unexplained 'waiting to send'. The worker sets a flag on the
    login-redirect break, ships it as blocked:"auth" in the queue-state
    message, and the page renders a login notice."""
    src = sw_source()
    body = js_function_body(src, "async function replayOnce()")
    assert "BLOCKED_AUTH = true" in body
    assert "BLOCKED_AUTH = false" in body, "flag must be recomputed on every run"
    state = js_function_body(src, "async function postQueueState()")
    assert "blocked:" in state
    partial = OFFLINE_PARTIAL.read_text()
    assert '"auth"' in partial
    assert "Signed out" in partial
    assert 'login.href = "/login"' in partial


def test_failed_entries_keep_payload_status_and_timestamp():
    """A parked entry is not a deleted entry: the body a tech typed survives,
    plus the status and when it died so the queue screen can explain itself."""
    body = js_function_body(sw_source(), "async function failEntry(")
    for field in ("url:", "body:", "status:", "failed_at:", "page:", "label:"):
        assert field in body, f"failed entries drop {field!r}"
    assert "FAILED_STORE" in body


def test_failed_store_upgrade_does_not_touch_queued_work():
    """The v1 -> v2 upgrade must ADD the failed store, never recreate `posts`
    (which would drop whatever a tech had queued at update time)."""
    body = js_function_body(sw_source(), "function openQueueDB()")
    assert "QUEUE_DB_VERSION" in body
    assert body.count("objectStoreNames.contains") == 2, \
        "both stores must be created behind a contains() guard"
    assert "deleteObjectStore" not in body


# --------------------------------------------------------------------------
# things the fix must not have broken
# --------------------------------------------------------------------------

def test_cache_versioning_and_activate_purge_still_work(client):
    """VERSION comes from __APP_VERSION__ at serve time and both cache names
    derive from it, so a release produces a new worker that purges the old
    caches on activate."""
    text = client.get("/sw.js").text
    assert "__APP_VERSION__" not in text
    assert 'const CACHE = "cpp-static-" + VERSION;' in text
    assert 'const PAGES_CACHE = "cpp-pages-" + VERSION;' in text
    purge = js_function_body(text, 'self.addEventListener("activate"')
    assert "k !== CACHE && k !== PAGES_CACHE" in purge
    assert "caches.delete(k)" in purge


def test_what_gets_queued_is_unchanged():
    """Only urlencoded POSTs to /visit/*/complete|skip are queued, and a queued
    POST still answers with the /?queued=1 redirect the Today page expects."""
    src = sw_source()
    body = js_function_body(src, "function isQueueableVisitPost(")
    assert 'url.pathname.startsWith("/visit/")' in body
    assert 'url.pathname.endsWith("/complete") || url.pathname.endsWith("/skip")' in body
    assert 'contentType.includes("application/x-www-form-urlencoded")' in body
    assert '"/?queued=1"' in src
    assert 'body: entry.body' in src, "the replayed POST must send the original body untouched"


def test_no_background_sync_reliance():
    """iOS Safari has no Background Sync API — activate/message/online remain
    the real replay triggers, with `sync` only a bonus."""
    src = sw_source()
    assert 'self.addEventListener("message"' in src
    assert "replay())" in js_function_body(src, 'self.addEventListener("activate"')


# --------------------------------------------------------------------------
# the queue screen + htmx failure surface (rendered page)
# --------------------------------------------------------------------------

def test_sw_exposes_the_queue_screen_message_api():
    """The screen is client-side only, so every operation it offers has to
    exist as a worker message."""
    body = js_function_body(sw_source(), 'self.addEventListener("message"')
    for kind in ("flush-queue", "queue-state", "queue-retry", "queue-discard", "queue-hint"):
        assert f'"{kind}"' in body, f"worker does not handle the {kind!r} message"


def test_queue_state_message_carries_rows_not_just_a_count():
    """The badge alone was the bug's cover story. The worker must ship the
    entries themselves so the screen can name each stop and explain failures."""
    body = js_function_body(sw_source(), "async function postQueueState()")
    for key in ("count:", "failed:", "pending:", "failedEntries:"):
        assert key in body
    assert "count: pending.length" in body, "`count` must keep meaning pending entries"
    describe = js_function_body(sw_source(), "function describe(")
    assert "body" not in describe, "queued bodies must not be shipped to the page"


def test_queued_entry_records_the_stop_page_for_the_pending_overlay():
    """The queued URL only has the visit id; the submitting page is
    /visit/<property>/<date>, which is the href Today renders for that stop."""
    src = sw_source()
    assert "function refPath(" in src
    assert "req.referrer" in src
    body = js_function_body(src, "async function handleQueueablePost(")
    assert "queueAdd(url.pathname + url.search, body," in body


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_every_base_template_ships_the_offline_partial(base):
    """All four shells must include it — a theme that dropped it would give a
    tech a badge with nothing behind it."""
    src = base.read_text()
    assert '{% include "_offline.html" %}' in src
    assert src.count("data-sync-open") == 2, \
        "both the mobile and desktop badges must open the queue screen"
    for badge in ("sync-badge", "sync-badge-desktop"):
        assert f'<button type="button" id="{badge}"' in src, \
            f"{badge} must be a real button — a <span> is not tappable or focusable"


@pytest.mark.parametrize("theme", sorted(ALLOWED_THEMES))
def test_queue_screen_and_htmx_handlers_render_in_every_theme(client, engine, theme):
    from sqlalchemy.orm import Session

    from app import models
    with Session(engine) as s:
        models.set_setting(s, "active_theme", theme)
        s.commit()

    html = client.get("/").text
    assert 'id="sync-sheet"' in html, f"{theme}: no queue screen in the page shell"
    assert 'id="sync-sheet-body"' in html
    assert "data-sync-open" in html
    assert "htmx:sendError" in html, f"{theme}: htmx send failures are still silent"
    assert "htmx:responseError" in html, f"{theme}: htmx error responses are still silent"


def test_queue_screen_offers_retry_and_a_confirmed_discard(client):
    """Discard destroys a tech's work, so it must take a deliberate second tap
    — and must not be confirm(), which blocks the whole page."""
    html = client.get("/").text
    assert '"Retry"' in html
    assert '"Discard"' in html
    assert "Tap again to discard" in html
    assert 'type: "queue-discard"' in html


def test_toast_never_uses_a_blocking_dialog():
    src = OFFLINE_PARTIAL.read_text()
    # Comment lines are stripped: the partial explains WHY it avoids these, and
    # that prose must not be mistaken for a call.
    code = re.sub(r"//[^\n]*", "", src)
    assert not re.search(r"(?<![\w.])alert\s*\(", code), "alert() blocks the page"
    assert not re.search(r"(?<![\w.])confirm\s*\(", code), "confirm() blocks the page"
    assert 'role="status"' in src and 'aria-live="polite"' in src


def test_photo_upload_failure_is_shown_in_place(client):
    """Photo uploads are multipart hx-posts the worker deliberately does not
    queue, so a photo taken with no signal is lost work unless the strip says so."""
    html = client.get("/").text
    assert "photo-strip" in html
    assert "Not uploaded — retake when online" in html


def test_pending_sync_overlay_is_wired_to_the_queued_pages(client):
    """Stops completed offline still render as due from PAGES_CACHE; the page
    must mark the matching cards so a tech does not redo them."""
    html = client.get("/").text
    assert "data-pending-sync" in html
    assert "pending-chip" in html
    assert "Pending sync" in html
    assert "don't do it again" in html or "don&#39;t do it again" in html

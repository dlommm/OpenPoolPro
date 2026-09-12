"""Behavioral regressions discovered during the production review."""
import json
import os
import shutil
import subprocess
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def require_node():
    """Locate Node for the service-worker behavioral tests.

    These execute sw.js rather than string-matching it, so they are the only
    real guard on offline replay -- the area that has already produced one
    High-severity data-loss bug. Skipping is fine on a dev box without Node,
    but a silent skip in CI means the guard quietly stops existing, so CI
    sets REQUIRE_JS_TESTS=1 and a missing Node fails there instead.
    """
    node = shutil.which("node")
    if node is None:
        if os.environ.get("REQUIRE_JS_TESTS") == "1":
            pytest.fail("REQUIRE_JS_TESTS=1 but Node.js is not installed")
        pytest.skip("Node.js required for service worker behavioral tests")
    return node


@pytest.mark.parametrize("suffix", ["", "/dismiss"])
def test_missing_visit_property_returns_404(client, suffix):
    url = f"/visit/999999/2026-07-13{suffix}"
    response = client.post(url) if suffix else client.get(url)
    assert response.status_code == 404


def test_dismiss_preserves_completed_visit_attribution(client, engine):
    with Session(engine) as session:
        customer = models.Customer(name="Audit")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 13),
                             status="done", completed_by_id=None, resolved=False)
        session.add(visit)
        session.commit()
        pid, vid = prop.id, visit.id
    assert client.post(f"/visit/{pid}/2026-07-13/dismiss").status_code == 409
    with Session(engine) as session:
        visit = session.get(models.Visit, vid)
        assert visit.completed_by_id is None
        assert visit.resolved is False
        assert visit.status == "done"


def test_sample_seed_does_not_change_existing_business(client, engine, monkeypatch):
    from scripts import seed_sample_data

    with Session(engine) as session:
        session.add(models.Customer(name="Real customer"))
        models.set_setting(session, "business_name", "Real business")
        session.commit()
    monkeypatch.setattr(seed_sample_data, "make_engine", lambda: engine)
    seed_sample_data.run()
    with Session(engine) as session:
        assert models.get_setting(session, "business_name", "") == "Real business"
        assert len(session.scalars(select(models.Customer)).all()) == 1
        assert models.get_setting(session, "sample_seeded", "") == ""


# Loads the real sw.js into a node:vm sandbox, applies `setup` (stubbing the
# IndexedDB-backed helpers), then evaluates `expr` and returns its resolved
# value. Executing the worker beats grepping it: every offline bug found so far
# was in behaviour, not in the presence of a string.
def run_worker(node, setup, expr):
    script = """
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({self: {addEventListener() {}}, URL});
vm.runInContext(fs.readFileSync('app/static/sw.js', 'utf8'), context);
vm.runInContext(process.argv[1], context);
vm.runInContext(process.argv[2], context)
    .then(result => process.stdout.write(JSON.stringify(result)));
"""
    result = subprocess.run([node, "-e", script, setup, expr],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


@pytest.mark.parametrize("path,status,deleted,failed,blocked", [
    ("/", 200, 1, 0, False),
    ("/login", 200, 0, 0, True),
    ("/login/2fa", 200, 0, 0, True),
    ("/account?force2fa=1", 200, 0, 0, True),
    ("/visit/1/complete", 429, 0, 0, False),
    ("/visit/1/complete", 408, 0, 0, False),
    ("/visit/1/complete", 503, 0, 0, False),
    ("/visit/1/complete", 409, 0, 1, False),
])
def test_worker_executes_retry_decisions(path, status, deleted, failed, blocked):
    node = require_node()
    script = """
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({self: {addEventListener() {}}, URL});
vm.runInContext(fs.readFileSync('app/static/sw.js', 'utf8'), context);
vm.runInContext(`
let deleted = 0, failed = 0;
// The entry is owned by the signed-in user: this test is about retry
// decisions, so ownership must be a non-factor here.
getIdentity = async () => 'u1';
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3', owner: 'u1'}];
queueDelete = async () => { deleted++; };
failEntry = async () => { failed++; };
postQueueState = async () => {};
fetch = async () => RESPONSE;
`, context);
context.RESPONSE = {url: 'https://pool.example' + process.argv[1],
    status: Number(process.argv[2]), ok: Number(process.argv[2]) === 200};
vm.runInContext('replayOnce().then(() => JSON.stringify([deleted, failed, BLOCKED_AUTH]))', context)
    .then(result => process.stdout.write(result));
"""
    result = subprocess.run([node, "-e", script, path, str(status)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == [deleted, failed, blocked]


# --- offline queue identity partitioning ----------------------------------
# IndexedDB is per-device, not per-login: on a shared phone, work queued offline
# by one tech must never replay under the next tech's session.

QUEUE_SETUP = """
let sent = [], deleted = 0, parked = [];
queueDelete = async () => { deleted++; };
failEntry = async (entry, status, reason) => { parked.push([status, reason || '']); };
postQueueState = async () => {};
fetch = async (url, opts) => {
  sent.push((opts.headers || {})['X-Queued-Owner'] || '');
  return {url: 'https://pool.example/', status: 200, ok: true};
};
"""


def test_worker_replays_own_work_stamped_with_its_owner():
    node = require_node()
    setup = QUEUE_SETUP + """
getIdentity = async () => 'u1';
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3', owner: 'u1'}];
"""
    sent, deleted, parked = run_worker(
        node, setup, "replayOnce().then(() => [sent, deleted, parked])")
    assert sent == ["u1"]      # replayed, and it tells the server who queued it
    assert deleted == 1
    assert parked == []


def test_worker_never_replays_another_users_queued_work():
    node = require_node()
    setup = QUEUE_SETUP + """
getIdentity = async () => 'u1';
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3', owner: 'u2'}];
"""
    sent, deleted, parked = run_worker(
        node, setup, "replayOnce().then(() => [sent, deleted, parked])")
    # Not sent under u1's session, and — just as important — not destroyed:
    # it is u2's unsynced field work and syncs when u2 signs back in.
    assert sent == []
    assert deleted == 0
    assert parked == []


def test_worker_parks_preidentity_entries_instead_of_guessing_an_owner():
    node = require_node()
    setup = QUEUE_SETUP + """
getIdentity = async () => 'u1';
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3'}];
"""
    sent, deleted, parked = run_worker(
        node, setup, "replayOnce().then(() => [sent, deleted, parked])")
    assert sent == []                            # never auto-attributed
    assert parked == [[0, "identity-unknown"]]   # kept, and explained on the queue screen


def test_worker_waits_rather_than_replaying_with_no_known_identity():
    node = require_node()
    setup = QUEUE_SETUP + """
getIdentity = async () => '';
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3', owner: 'u1'}];
"""
    sent, deleted, parked = run_worker(
        node, setup, "replayOnce().then(() => [sent, deleted, parked])")
    assert (sent, deleted, parked) == ([], 0, [])


def test_worker_drains_own_entries_past_a_colleagues():
    node = require_node()
    setup = QUEUE_SETUP + """
getIdentity = async () => 'u1';
queueAll = async () => [
  {id: 1, url: '/visit/1/complete', body: 'a', owner: 'u2'},
  {id: 2, url: '/visit/2/complete', body: 'b', owner: 'u1'},
];
"""
    sent, deleted, parked = run_worker(
        node, setup, "replayOnce().then(() => [sent, deleted, parked])")
    # A colleague's entry at the head must not gate this user's queue forever.
    assert sent == ["u1"]
    assert deleted == 1


def test_switching_user_clears_cached_pages_but_keeps_queued_work():
    node = require_node()
    setup = """
let cleared = 0, stored = '', replayed = 0;
getIdentity = async () => 'u1';
setIdentity = async (v) => { stored = v; };
clearPagesCache = async () => { cleared++; };
replay = async () => { replayed++; };
"""
    # A different person signs in: the previous user's rendered pages (gate
    # codes, dog notes) must stop being readable offline on this device.
    cleared, stored, replayed = run_worker(
        node, setup, "applyIdentity('u2').then(() => [cleared, stored, replayed])")
    assert (cleared, stored, replayed) == (1, "u2", 1)


def test_same_user_reloading_does_not_clear_cached_pages():
    node = require_node()
    setup = """
let cleared = 0, replayed = 0;
getIdentity = async () => 'u1';
setIdentity = async () => {};
clearPagesCache = async () => { cleared++; };
replay = async () => { replayed++; };
"""
    cleared, replayed = run_worker(
        node, setup, "applyIdentity('u1').then(() => [cleared, replayed])")
    assert cleared == 0    # every page load would otherwise wipe the offline cache
    assert replayed == 1


def test_retrying_a_parked_entry_claims_it_for_the_current_user():
    node = require_node()
    setup = """
let queued = [];
getIdentity = async () => 'u2';
storeGet = async () => ({id: 5, url: '/visit/1/complete', body: 'fc=3', owner: ''});
queueAdd = async (url, body, page, label, owner) => { queued.push(owner); };
storeDelete = async () => {};
replay = async () => {};
"""
    queued = run_worker(node, setup, "retryFailed(5).then(() => queued)")
    # Explicit human action is what makes attribution safe.
    assert queued == ["u2"]


def test_queue_state_separates_a_colleagues_work_from_your_own():
    node = require_node()
    setup = """
let posted = null;
getIdentity = async () => 'u1';
queueAll = async () => [
  {id: 1, url: '/visit/1/complete', owner: 'u1', ts: 1},
  {id: 2, url: '/visit/2/complete', owner: 'u2', ts: 2},
];
failedAll = async () => [];
self.clients = {matchAll: async () => [{postMessage: (m) => { posted = m; }}]};
"""
    posted = run_worker(node, setup, "postQueueState().then(() => posted)")
    assert posted["count"] == 1      # the badge counts only what this login can send
    assert posted["foreign"] == 1
    assert [e["id"] for e in posted["pending"]] == [1]


def test_replayed_visit_from_another_user_is_refused(client, engine):
    with Session(engine) as session:
        customer = models.Customer(name="Shared device")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 13), status="due")
        session.add(visit)
        session.commit()
        vid = visit.id
    # The worker skips these client-side; this is the server-side guarantee that
    # a stale or tampered worker cannot misattribute a completion either.
    response = client.post(f"/visit/{vid}/complete", data={"form_status": "due", "fc": "3"},
                           headers={"X-Queued-Owner": "999999"})
    assert response.status_code == 409
    with Session(engine) as session:
        assert session.get(models.Visit, vid).status == "due"   # nothing written


@pytest.mark.parametrize("headers", [{}, None])
def test_replayed_visit_from_its_own_queuer_is_accepted(client, engine, headers):
    with Session(engine) as session:
        customer = models.Customer(name="Own device")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 14), status="due")
        session.add(visit)
        session.commit()
        vid = visit.id
    with Session(engine) as session:
        me = session.scalars(select(models.User)).first()
        sent = {} if headers is None else {"X-Queued-Owner": str(me.id)}
    # headers=None exercises the pre-upgrade path: an entry queued by an older
    # worker sends no header at all and must still go through.
    response = client.post(f"/visit/{vid}/complete", data={"form_status": "due", "fc": "3"},
                           headers=sent)
    assert response.status_code < 400
    with Session(engine) as session:
        assert session.get(models.Visit, vid).status == "done"


# --- concurrent writes ----------------------------------------------------
# "Single process" is not "single request": handlers still interleave. A guard
# that compares values already read into Python cannot see a second writer.

def test_second_completion_of_the_same_revision_is_refused(client, engine):
    with Session(engine) as session:
        customer = models.Customer(name="Race")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 15), status="due")
        session.add(visit)
        session.commit()
        vid, rev = visit.id, visit.revision

    first = client.post(f"/visit/{vid}/complete",
                        data={"form_status": "due", "revision": str(rev), "fc": "3.0"})
    assert first.status_code < 400
    # Both requests were rendered from the same revision — exactly the case the
    # status check misses once the first write has landed.
    second = client.post(f"/visit/{vid}/complete",
                         data={"form_status": "due", "revision": str(rev), "fc": "9.9"})
    assert second.status_code == 409
    with Session(engine) as session:
        assert session.get(models.Visit, vid).fc == 3.0   # first writer's readings survive


def test_editing_an_already_done_visit_still_detects_a_concurrent_edit(client, engine):
    """The gap the status guard cannot close: both edits start from "done", so
    form_status matches for both and only the revision separates them."""
    with Session(engine) as session:
        customer = models.Customer(name="Both done")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 16), status="done", fc=1.0)
        session.add(visit)
        session.commit()
        vid, rev = visit.id, visit.revision

    ok = client.post(f"/visit/{vid}/complete",
                     data={"form_status": "done", "revision": str(rev), "fc": "2.0"})
    assert ok.status_code < 400
    stale = client.post(f"/visit/{vid}/complete",
                        data={"form_status": "done", "revision": str(rev), "fc": "3.0"})
    assert stale.status_code == 409
    with Session(engine) as session:
        assert session.get(models.Visit, vid).fc == 2.0


def test_completion_without_a_revision_field_still_works(client, engine):
    """Offline entries queued by an older build carry no revision; rejecting
    them would strand real field work."""
    with Session(engine) as session:
        customer = models.Customer(name="Legacy form")
        prop = models.Property(customer=customer)
        visit = models.Visit(property=prop, date=date(2026, 7, 17), status="due")
        session.add(visit)
        session.commit()
        vid = visit.id
    response = client.post(f"/visit/{vid}/complete", data={"form_status": "due", "fc": "4.0"})
    assert response.status_code < 400
    with Session(engine) as session:
        assert session.get(models.Visit, vid).status == "done"


def test_visit_form_renders_the_revision_it_was_built_from(client, engine):
    """If this hidden field ever stops rendering, claim_visit() takes its
    "form predates the field" path and every completion is allowed again --
    the protection would disappear silently, with all tests still green."""
    with Session(engine) as session:
        customer = models.Customer(name="Form render")
        prop = models.Property(customer=customer)
        session.add(models.Visit(property=prop, date=date(2026, 7, 18), status="due"))
        session.commit()
        pid = prop.id
    body = client.get(f"/visit/{pid}/2026-07-18").text
    assert 'name="revision"' in body


def test_concurrent_purchases_do_not_lose_stock(client, engine):
    """Two purchases of one chemical from separately-loaded ORM objects: a
    read-modify-write would apply only the second. (`client` builds the schema.)"""
    from app import inventory

    with Session(engine) as session:
        chem = models.Chemical(name="Chlorine", unit="gal", stock_qty=10.0,
                               cost_per_unit_cents=100)
        session.add(chem)
        session.commit()
        cid = chem.id

    with Session(engine) as first, Session(engine) as second:
        a = first.get(models.Chemical, cid)
        b = second.get(models.Chemical, cid)   # both hold stock_qty == 10.0
        inventory.record_purchase(first, a, 5.0, 500, "a", None)
        first.commit()
        inventory.record_purchase(second, b, 3.0, 300, "b", None)
        second.commit()

    with Session(engine) as session:
        assert session.get(models.Chemical, cid).stock_qty == 18.0   # 10 + 5 + 3


@pytest.mark.parametrize("value", ["inf", "nan", "1e308", "1e20", "-10"])
def test_money_parser_rejects_unstorable_amounts(value):
    from app.forms import cents
    assert cents(value) is None


@pytest.mark.parametrize("value", ["inf", "1e308", "-10"])
def test_csv_import_handles_invalid_rate(client, engine, value):
    import re
    response = client.post('/customers/import', files={
        'file': ('audit.csv', f'name,address,rate\nAudit,1 Main St,{value}\n'.encode(), 'text/csv')})
    token = re.search(r'/customers/import/([a-f0-9]{32})/preview', response.text).group(1)
    response = client.post(f'/customers/import/{token}/commit', data={
        'map_customer_name': 'name', 'map_address': 'address', 'map_rate_override_dollars': 'rate'})
    assert response.status_code == 200
    with Session(engine) as session:
        prop = session.scalars(select(models.Property)).one()
        assert prop.rate_override_cents is None


@pytest.mark.parametrize('path', ['/account', '/settings', '/users', '/login', '/sign/invalid'])
def test_sensitive_responses_are_not_stored_in_http_cache(client, path):
    response = client.get(path)
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('cache_control,expected', [('no-store', 0), ('no-cache', 1)])
def test_worker_respects_server_cache_policy(cache_control, expected):
    node = require_node()
    script = """
const vm = require('node:vm');
const fs = require('node:fs');
let writes = 0;
const response = {url: 'https://pool.example/account', ok: true,
    headers: {get: () => process.argv[1]}, clone: () => ({})};
const context = vm.createContext({self: {addEventListener() {}}, URL,
    fetch: async () => response,
    caches: {open: async () => ({put: async () => { writes++; }})}});
vm.runInContext(fs.readFileSync('app/static/sw.js', 'utf8'), context);
vm.runInContext("handleNavigate({url: 'https://pool.example/dashboard'})", context)
    .then(() => process.stdout.write(String(writes)));
"""
    result = subprocess.run([node, '-e', script, cache_control], capture_output=True,
                            text=True, check=True, timeout=10)
    assert int(result.stdout) == expected

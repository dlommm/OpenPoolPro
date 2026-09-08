"""Behavioral regressions discovered during the production review."""
import json
import shutil
import subprocess
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


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
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for service worker behavioral tests")
    script = """
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({self: {addEventListener() {}}, URL});
vm.runInContext(fs.readFileSync('app/static/sw.js', 'utf8'), context);
vm.runInContext(`
let deleted = 0, failed = 0;
queueAll = async () => [{id: 1, url: '/visit/1/complete', body: 'fc=3'}];
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
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required for service worker behavioral tests')
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

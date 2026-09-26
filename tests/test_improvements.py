import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, followups, mailer, models, operations, portal, route_planning, water_alerts
from app.observability import logged_job, redact_path
from app.timefmt import business_today


def seed(engine, **kwargs):
    with Session(engine) as s:
        customer = models.Customer(name="Portal Customer", email="customer@example.test", notes="PRIVATE CUSTOMER NOTE")
        prop = models.Property(customer=customer, label="Home", address="1 Main St", gate_code="SECRET GATE",
                               notes="PRIVATE PROPERTY NOTE", **kwargs)
        s.add(prop)
        s.commit()
        return customer.id, prop.id


def invoice(engine, customer_id, status="sent", overdue=10):
    with Session(engine) as s:
        inv = models.Invoice(customer_id=customer_id, number=f"AUDIT-{customer_id}", total_cents=12500,
            period_year=2026, period_month=9, status=status, pdf_filename=f"inv-{customer_id}.pdf",
            due_date=business_today()-timedelta(days=overdue))
        s.add(inv); s.commit()
        return inv.id


def token_for(engine, customer_id):
    with Session(engine) as s:
        token = portal.issue(s, customer_id)
        s.commit()
        return token


@pytest.mark.parametrize("path", ["/operations", "/routes/plan", "/followups", "/water-alerts"])
def test_new_screens_render_and_require_permission(client, tech_client, path):
    assert client.get(path).status_code == 200
    assert tech_client().get(path).status_code == 403


def test_route_suggestion_is_shorter_and_preserves_unknown_slot():
    points = [models.Property(id=i, latitude=0, longitude=lon) for i, lon in enumerate([0, 3, 1, 2], 1)]
    suggested = route_planning.suggest(points)
    assert [p.id for p in suggested] == [1, 3, 4, 2]
    assert route_planning.estimate(suggested)['miles'] < route_planning.estimate(points)['miles']
    points.insert(2, models.Property(id=99))
    assert route_planning.suggest(points)[2].id == 99
    assert route_planning.estimate(points) is None
    assert route_planning.coordinates(models.Property(latitude=float('nan'), longitude=3)) is None
    assert route_planning.coordinates(models.Property(latitude=100, longitude=3)) is None


def test_route_order_save_and_stale_rejection(client, engine):
    _, p1 = seed(engine, weekday=0, route_order=1, latitude=0, longitude=1)
    _, p2 = seed(engine, weekday=0, route_order=2, latitude=0, longitude=2)
    page = client.get('/routes/plan').text
    fingerprint = re.search(r'name="fingerprint" value="([a-f0-9]+)"', page).group(1)
    data = {'weekday': '0', 'tech_id': '0', 'fingerprint': fingerprint, 'property_id': [str(p2), str(p1)]}
    assert client.post('/routes/plan', data=data, follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert [p.id for p in route_planning.properties(s, 0, 0)] == [p2, p1]
    assert client.post('/routes/plan', data=data).status_code == 409
    fresh = client.get('/routes/plan').text
    data['fingerprint'] = re.search(r'name="fingerprint" value="([a-f0-9]+)"', fresh).group(1)
    data['property_id'] = [str(p1), str(p1)]
    assert client.post('/routes/plan', data=data).status_code == 400
    data['property_id'] = ['bad']
    assert client.post('/routes/plan', data=data).status_code == 400
    assert client.get('/routes/plan?weekday=99').status_code == 422


def test_portal_issue_requires_https_config_and_is_revocable(client, engine):
    cid, _ = seed(engine)
    assert client.post(f'/customers/{cid}/portal/issue').status_code == 400
    with Session(engine) as s:
        models.set_setting(s, 'public_base_url', 'https://pool.example.test'); s.commit()
    response = client.post(f'/customers/{cid}/portal/issue', data={'days': 7})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    token = re.search(r'https://pool.example.test/portal/([\w-]+)', response.text).group(1)
    with Session(engine) as s:
        assert s.get(models.PortalAccess, cid).token_hash != token
    client.cookies.clear()
    assert client.get(f'/portal/{token}').status_code == 200
    assert client.get('/customers').status_code == 200  # follows the staff login redirect
    assert 'Wrong username' not in client.get('/login').text


def test_portal_filters_private_fields_and_other_customers(client, engine):
    cid, pid = seed(engine)
    other, _ = seed(engine)
    iid = invoice(engine, cid)
    other_iid = invoice(engine, other)
    with Session(engine) as s:
        s.add(models.Visit(property_id=pid, date=business_today(), status='done', fc=3.5,
                           notes='PRIVATE VISIT NOTE', tasks_done=['Brush pool']))
        s.commit()
    root = Path(config.DATA_DIR) / 'invoices'; root.mkdir()
    (root / f'inv-{cid}.pdf').write_bytes(b'%PDF-1.4 customer')
    token = token_for(engine, cid)
    client.cookies.clear()
    response = client.get(f'/portal/{token}')
    assert response.status_code == 200
    for secret in ['PRIVATE CUSTOMER NOTE', 'PRIVATE PROPERTY NOTE', 'PRIVATE VISIT NOTE', 'SECRET GATE']:
        assert secret not in response.text
    assert 'Brush pool' in response.text and '3.5' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'serviceWorker' not in response.text
    assert client.get(f'/portal/{token}/invoices/{iid}').content == b'%PDF-1.4 customer'
    assert client.get(f'/portal/{token}/invoices/{other_iid}').status_code == 404
    assert client.get(f'/portal/{token}/invoices/99999').status_code == 404
    assert redact_path(f'/portal/{token}/invoices/{iid}') == f'/portal/[token]/invoices/{iid}'
    with Session(engine) as s:
        s.get(models.Invoice, iid).status = 'draft'; s.commit()
    assert client.get(f'/portal/{token}/invoices/{iid}').status_code == 404
    with Session(engine) as s:
        s.get(models.PortalAccess, cid).expires_at = models.utcnow() - timedelta(seconds=1); s.commit()
    assert client.get(f'/portal/{token}').status_code == 404


def test_portal_rotation_revoke_inactive_and_invalid(client, engine):
    cid, _ = seed(engine)
    first = token_for(engine, cid)
    second = token_for(engine, cid)
    assert client.get(f'/portal/{first}').status_code == 404
    assert client.get(f'/portal/{second}').status_code == 200
    assert client.get('/portal/invalid').status_code == 404
    assert client.post(f'/customers/{cid}/portal/revoke', follow_redirects=False).status_code == 303
    assert client.get(f'/portal/{second}').status_code == 404
    third = token_for(engine, cid)
    with Session(engine) as s:
        s.get(models.Customer, cid).active = False; s.commit()
    assert client.get(f'/portal/{third}').status_code == 404
    assert client.post(f'/customers/{cid}/portal/issue').status_code == 409


def test_portal_photos_require_explicit_sharing_and_ownership(client, engine):
    cid, pid = seed(engine)
    other, other_pid = seed(engine)
    with Session(engine) as s:
        photo = models.Photo(entity_type='property', entity_id=pid, filename='photo.jpg', thumb_filename='thumb.jpg')
        stranger = models.Photo(entity_type='property', entity_id=other_pid,
                                filename='photo.jpg', thumb_filename='t.jpg')
        s.add_all([photo, stranger]); s.commit(); photo_id, stranger_id = photo.id, stranger.id
    root = Path(config.DATA_DIR) / 'photos' / 'property' / str(pid); root.mkdir(parents=True)
    (root / 'photo.jpg').write_bytes(b'JPEG')
    token = token_for(engine, cid)
    assert client.get(f'/portal/{token}/photos/{photo_id}').status_code == 404
    assert client.post(f'/customers/{cid}/portal/photos/{stranger_id}', data={'shared': 'true'}).status_code == 404
    assert client.post(f'/customers/{cid}/portal/photos/{photo_id}', data={'shared': 'true'}).status_code == 200
    assert client.get(f'/portal/{token}/photos/{photo_id}').content == b'JPEG'
    assert client.get(f'/portal/{token}/photos/{stranger_id}').status_code == 404
    assert client.post(f'/customers/{cid}/portal/photos/{photo_id}', data={'shared': 'false'}).status_code == 200
    assert client.get(f'/portal/{token}/photos/{photo_id}').status_code == 404
    assert client.get(f'/customers/{cid}/portal').status_code == 200


def test_portal_contract_scope_and_signed_copy(client, engine):
    cid, pid = seed(engine)
    other, other_pid = seed(engine)
    with Session(engine) as s:
        pool = models.Pool(property_id=pid); other_pool = models.Pool(property_id=other_pid)
        s.add_all([pool, other_pool]); s.flush()
        contract = models.Contract(pool_id=pool.id, season_year=2026, status='signed')
        stranger = models.Contract(pool_id=other_pool.id, season_year=2026, status='sent')
        s.add_all([contract, stranger]); s.commit(); contract_id, stranger_id = contract.id, stranger.id
    root = Path(config.DATA_DIR) / 'contracts' / str(contract_id); root.mkdir(parents=True)
    (root / 'signed.pdf').write_bytes(b'SIGNED')
    (root / 'contract.pdf').write_bytes(b'UNSIGNED')
    token = token_for(engine, cid)
    assert client.get(f'/portal/{token}/contracts/{contract_id}').content == b'SIGNED'
    assert client.get(f'/portal/{token}/contracts/{stranger_id}').status_code == 404
    with Session(engine) as s:
        s.get(models.Contract, contract_id).status = 'draft'; s.commit()
    assert client.get(f'/portal/{token}/contracts/{contract_id}').status_code == 404


def test_followups_preview_approval_duplicate_and_delivery(client, engine, monkeypatch):
    cid, _ = seed(engine); iid = invoice(engine, cid)
    page = client.get('/followups')
    assert 'customer@example.test' in page.text and '$125.00' in page.text
    with Session(engine) as s:
        rows = followups.candidates(s, business_today()); fingerprint = rows[0]['snapshot']
        assert s.scalars(select(models.EmailOutbox)).all() == []
    assert client.post('/followups/approve', data={'snapshot': fingerprint}, follow_redirects=False).status_code == 303
    assert client.post('/followups/approve', data={'snapshot': fingerprint}).status_code == 409
    monkeypatch.setattr(mailer, 'smtp_configured', lambda s: True)
    monkeypatch.setattr(mailer, 'send_conf', lambda s: {})
    sent = []; monkeypatch.setattr(mailer, 'send_message', lambda *args: sent.append(args))
    with Session(engine) as s:
        assert mailer.flush_outbox(s) == (1, 0)
        assert s.scalars(select(models.InvoiceFollowup)).one().invoice_id == iid
        assert followups.candidates(s, business_today()) == []
    assert len(sent) == 1


@pytest.mark.parametrize('change', ['paid', 'partial', 'void', 'email', 'inactive'])
def test_followups_suppress_changes_after_approval(client, engine, monkeypatch, change):
    cid, _ = seed(engine); iid = invoice(engine, cid)
    with Session(engine) as s:
        row = followups.candidates(s, business_today())[0]
        owner = s.scalar(select(models.User).where(models.User.username == 'owner'))
        followups.approve(s, row, owner.id); s.commit()
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        if change in ('paid', 'partial'):
            s.add(models.Payment(invoice_id=iid, amount_cents=12500 if change == 'paid' else 100,
                received_on=business_today(), method='cash'))
        elif change == 'void':
            inv.status = 'void'
        elif change == 'email':
            inv.customer.email = 'different@example.test'
        else:
            inv.customer.active = False
        s.commit()
    monkeypatch.setattr(mailer, 'smtp_configured', lambda s: True)
    monkeypatch.setattr(mailer, 'send_conf', lambda s: {})
    monkeypatch.setattr(mailer, 'send_message', lambda *args: pytest.fail('Canceled reminder was sent'))
    with Session(engine) as s:
        assert mailer.flush_outbox(s) == (0, 0)
        assert s.scalars(select(models.EmailOutbox)).one().status == 'canceled'


def test_followups_reject_changed_preview_and_validate_settings(client, engine):
    cid, _ = seed(engine); iid = invoice(engine, cid)
    with Session(engine) as s:
        fingerprint = followups.candidates(s, business_today())[0]['snapshot']
        s.get(models.Invoice, iid).total_cents = 99; s.commit()
    assert client.post('/followups/approve', data={'snapshot': fingerprint}).status_code == 409
    assert client.post('/followups/approve').status_code == 400
    for days in ['0,14', 'a', '1,2,3,4,5,6,7']:
        assert client.post('/followups/settings', data={'days': days}).status_code == 400
    assert client.post('/followups/settings', data={'days': '5,15,40'}).status_code == 200
    with Session(engine) as s:
        assert followups.stages(s) == [5, 15, 40]
        models.set_setting(s, 'followup_days', 'broken'); s.commit()
        assert followups.stages(s) == [7, 14, 30]


def test_water_alert_creates_one_inspection_and_clears_when_corrected(client, engine):
    _, pid = seed(engine)
    with Session(engine) as s:
        for i, ph in enumerate([8.0, 8.2, 8.3]):
            s.add(models.Visit(property_id=pid, date=business_today()-timedelta(days=14-i*7), status='done', ph=ph))
        s.commit()
        alerts = water_alerts.current(s, business_today())
        assert len(alerts) == 1
        fingerprint = alerts[0]['fingerprint']
    assert 'Repeated readings' in client.get('/water-alerts').text
    first = client.post('/water-alerts/inspect', data={'fingerprint': fingerprint}, follow_redirects=False)
    assert first.status_code == 303
    again = client.post('/water-alerts/inspect', data={'fingerprint': fingerprint}, follow_redirects=False)
    assert again.headers['location'] == first.headers['location']
    with Session(engine) as s:
        assert len(s.scalars(select(models.Job)).all()) == 1
        assert len(s.scalars(select(models.JobTask)).all()) == 1
        latest = s.scalars(select(models.Visit).order_by(models.Visit.date.desc())).first()
        latest.ph = 7.5; s.commit()
        assert water_alerts.current(s, business_today()) == []
    assert client.post('/water-alerts/inspect', data={'fingerprint': fingerprint}).status_code == 409


def test_water_alert_ignores_old_data_nonfinite_and_non_salt_pools(client, engine):
    _, pid = seed(engine)
    with Session(engine) as s:
        for i in range(3):
            s.add(models.Visit(property_id=pid, date=business_today()-timedelta(days=100+i), status='done', ph=9))
        for i in range(2):
            s.add(models.Visit(property_id=pid, date=business_today()-timedelta(days=i), status='done', salt=100,
                               fc=float('inf')))
        s.commit()
        assert water_alerts.current(s, business_today()) == []
        s.add(models.Pool(property_id=pid, water_type='salt')); s.commit()
        assert water_alerts.current(s, business_today())[0]['metric'] == 'salt'


def test_operations_records_job_success_and_failure(client, engine, monkeypatch):
    assert logged_job(lambda engine: 42, 'test_ok')(engine) == 42
    def fail(engine):
        raise RuntimeError('PRIVATE TOKEN')
    assert logged_job(fail, 'test_fail')(engine) is None
    with Session(engine) as s:
        assert s.get(models.OperationRun, 'test_ok').status == 'finished'
        assert s.get(models.OperationRun, 'test_fail').status == 'failed'
    page = client.get('/operations')
    assert 'test_fail' in page.text and 'PRIVATE TOKEN' not in page.text
    monkeypatch.setattr(operations.shutil, 'disk_usage', lambda _: (_ for _ in ()).throw(OSError()))
    assert 'Storage unavailable' in client.get('/operations').text


def test_portal_staff_controls_are_admin_only(client, tech_client, engine):
    cid, _ = seed(engine)
    tech = tech_client(manage_customers=True)
    assert tech.get(f'/customers/{cid}/portal').status_code == 403
    assert tech.post(f'/customers/{cid}/portal/issue').status_code == 403
    assert tech.post(f'/customers/{cid}/portal/revoke').status_code == 403


# ---------- error pages ----------

def test_a_failed_staff_action_renders_a_page_not_a_json_blob(client, engine):
    """Regression: "Create portal link" without an HTTPS base URL answered a
    legitimate setup problem with {"detail": "..."} filling the whole window.
    The 422 handler already treated browser posts this way; 4xx/5xx did not.
    """
    cid, _ = seed(engine)
    r = client.post(f"/customers/{cid}/portal/issue", data={"days": 7},
                    headers={"Accept": "text/html"})
    assert r.status_code == 400
    assert not r.text.lstrip().startswith("{"), "raw JSON reached the browser"
    assert "<html" in r.text
    # The detail these routes raise is written for an operator -- show it.
    assert "HTTPS Public base URL" in r.text
    assert "Go back" in r.text


def test_non_browser_callers_still_get_structured_errors(client, engine):
    """htmx and API clients parse the body; a rendered page would break them."""
    cid, _ = seed(engine)
    for headers in ({"Accept": "application/json"}, {"Accept": "text/html", "HX-Request": "true"}):
        r = client.post(f"/customers/{cid}/portal/issue", data={"days": 7}, headers=headers)
        assert r.status_code == 400
        assert r.json()["detail"]


# ---------- the customer's own page ----------

def test_portal_shows_dates_the_way_a_customer_reads_them(client, engine):
    """The portal is the one page a customer ever sees. A bare ISO date is a
    developer's format, and every visit heading carried one."""
    from datetime import date

    cid, pid = seed(engine)
    with Session(engine) as s:
        s.add(models.Visit(property_id=pid, date=date(2026, 9, 17), status="done",
                           fc=3.4, ph=7.6))
        s.commit()
    body = client.get(f"/portal/{token_for(engine, cid)}").text
    assert "Sep 17, 2026" in body
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", body), "bare ISO date on the portal"


def test_portal_hides_the_quickbooks_section_when_there_is_nothing_in_it(client, engine):
    """A company that does not bill through QuickBooks should not show every
    customer a second, permanently empty invoice heading."""
    cid, _ = seed(engine)
    body = client.get(f"/portal/{token_for(engine, cid)}").text
    assert "QuickBooks invoices" not in body
    assert body.count("Invoices") >= 1  # the local section still has its heading


def test_an_invalid_portal_link_is_a_finished_page(client, engine):
    r = client.get("/portal/not-a-real-token")
    assert r.status_code == 404
    assert "Customer portal" in r.text
    # No route back into the app: a customer holding a dead link has no account.
    assert 'href="/"' not in r.text


def test_failed_offbox_sync_is_surfaced_in_settings_and_operations(client, engine, monkeypatch):
    monkeypatch.setattr(config, 'BACKUP_SYNC_CMD', 'rclone copy "$BACKUP_FILE" remote:')
    assert 'Configured; verify copies at destination' in client.get('/operations').text
    with Session(engine) as s:
        models.set_setting(s, 'last_backup_sync_error', 'CalledProcessError (exit 3)')
        s.commit()
    assert 'Off-box copy: Last attempt failed' in client.get('/operations').text
    assert 'Off-box backup sync failed (CalledProcessError (exit 3))' in client.get('/settings').text

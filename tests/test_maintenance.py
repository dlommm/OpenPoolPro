import signal
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import Base
from app.maintenance import run_maintenance_rules, run_nightly, warranty_alerts


class _HangGuard:
    """Fails fast instead of hanging the whole suite if a regression brings
    back the infinite catch-up loop for a non-positive interval_weeks rule."""

    def __init__(self, seconds=5):
        self.seconds = seconds

    def __enter__(self):
        def _on_alarm(signum, frame):
            raise TimeoutError(
                "run_maintenance_rules did not return in time -- "
                "likely the interval_weeks<=0 catch-up loop regressed to an infinite loop")
        self._old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._old)


def _customer_property(s, active=True, cust_active=True):
    c = models.Customer(name="Cust", active=cust_active)
    p = models.Property(customer=c, active=active)
    s.add(p)
    s.commit()
    return p


def _pool_equipment(s, cust_active=True, prop_active=True, warranty_end=None, kind="pump"):
    c = models.Customer(name="WarrCust", active=cust_active)
    p = models.Property(customer=c, active=prop_active)
    s.add(p)
    s.commit()
    pool = models.Pool(property_id=p.id)
    s.add(pool)
    s.commit()
    e = models.Equipment(pool_id=pool.id, kind=kind, warranty_end=warranty_end)
    s.add(e)
    s.commit()
    return e


# --- rules engine golden path -------------------------------------------------

def test_rule_due_creates_job_with_scheduled_date_and_rule_id(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        rule = models.MaintenanceRule(property_id=p.id, title="Filter clean",
                                      next_due=today + timedelta(days=3),
                                      lead_days=7, interval_weeks=4)
        s.add(rule)
        s.commit()
        created = run_maintenance_rules(s, today)
        assert created == 1
        job = s.scalars(select(models.Job)).one()
        assert job.rule_id == rule.id
        assert job.scheduled_date == today + timedelta(days=3)
        assert job.status == "scheduled"
        assert job.job_type == rule.job_type
        assert job.assigned_tech_id is None


def test_second_run_same_day_no_duplicate(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        s.add(models.MaintenanceRule(property_id=p.id, title="Filter clean",
                                      next_due=today + timedelta(days=3),
                                      lead_days=7, interval_weeks=4))
        s.commit()
        first = run_maintenance_rules(s, today)
        second = run_maintenance_rules(s, today)
        assert first == 1
        assert second == 0
        assert len(s.scalars(select(models.Job)).all()) == 1


def test_job_done_next_cycle_creates_again(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        rule = models.MaintenanceRule(property_id=p.id, title="Filter clean",
                                      next_due=today + timedelta(days=3),
                                      lead_days=7, interval_weeks=1)
        s.add(rule)
        s.commit()
        run_maintenance_rules(s, today)
        job1 = s.scalars(select(models.Job)).one()
        assert job1.scheduled_date == today + timedelta(days=3)
        job1.status = "done"
        s.commit()
        cycle2_due = rule.next_due  # advanced by the first run's catch-up loop
        later = cycle2_due - timedelta(days=rule.lead_days)
        created = run_maintenance_rules(s, later)
        assert created == 1
        jobs = s.scalars(select(models.Job).order_by(models.Job.id)).all()
        assert len(jobs) == 2
        assert jobs[1].rule_id == rule.id
        assert jobs[1].scheduled_date == cycle2_due
        assert jobs[1].status == "scheduled"


def test_catchup_creates_one_job_and_advances_next_due_into_future(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        rule = models.MaintenanceRule(property_id=p.id, title="Filter clean",
                                      next_due=today - timedelta(weeks=10),
                                      lead_days=7, interval_weeks=4)
        s.add(rule)
        s.commit()
        created = run_maintenance_rules(s, today)
        assert created == 1
        assert len(s.scalars(select(models.Job)).all()) == 1
        job = s.scalars(select(models.Job)).one()
        assert job.scheduled_date == today - timedelta(weeks=10)  # original due date preserved
        assert rule.next_due - timedelta(days=rule.lead_days) > today
        assert rule.next_due > today


def test_inactive_rule_skipped(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        s.add(models.MaintenanceRule(property_id=p.id, title="X", next_due=today,
                                      lead_days=7, active=False))
        s.commit()
        created = run_maintenance_rules(s, today)
        assert created == 0
        assert s.scalars(select(models.Job)).first() is None


def test_inactive_property_skipped(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s, active=False)
        s.add(models.MaintenanceRule(property_id=p.id, title="X", next_due=today, lead_days=7))
        s.commit()
        created = run_maintenance_rules(s, today)
        assert created == 0
        assert s.scalars(select(models.Job)).first() is None


def test_inactive_customer_property_skipped(engine):
    # Property itself is still active, but its owning customer was
    # deactivated (deactivate_customer doesn't cascade to properties) --
    # the rules engine must skip it anyway, consistent with stops_for_date
    # and warranty_alerts, which both filter on Customer.active.
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s, active=True, cust_active=False)
        s.add(models.MaintenanceRule(property_id=p.id, title="X", next_due=today, lead_days=7))
        s.commit()
        created = run_maintenance_rules(s, today)
        assert created == 0
        assert s.scalars(select(models.Job)).first() is None


@pytest.mark.parametrize("interval_weeks", [0, -1, -4])
def test_nonpositive_interval_skipped_no_hang(engine, interval_weeks):
    # Nothing in the schema stops interval_weeks from being <= 0 (and a
    # later task adds free-form form input for it), so a misconfigured rule
    # must be skipped outright rather than spinning the catch-up loop
    # forever -- that would hang the nightly scheduler thread or the
    # run-now HTTP request. Guarded with an alarm so a regression fails
    # fast instead of hanging the whole test run.
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        p = _customer_property(s)
        rule = models.MaintenanceRule(property_id=p.id, title="Broken rule",
                                      next_due=today - timedelta(weeks=10),
                                      lead_days=7, interval_weeks=interval_weeks)
        s.add(rule)
        s.commit()
        rule_id = rule.id
        original_next_due = rule.next_due

        with _HangGuard(seconds=5):
            created = run_maintenance_rules(s, today)

        assert created == 0
        assert s.scalars(select(models.Job)).first() is None
        assert s.get(models.MaintenanceRule, rule_id).next_due == original_next_due


# --- warranty alerts -----------------------------------------------------------

def test_warranty_alerts_window_incl_past_due(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        past = _pool_equipment(s, warranty_end=today - timedelta(days=5))
        soon = _pool_equipment(s, warranty_end=today + timedelta(days=59))
        exact = _pool_equipment(s, warranty_end=today + timedelta(days=60))
        far = _pool_equipment(s, warranty_end=today + timedelta(days=61))
        none_end = _pool_equipment(s, warranty_end=None)
        ids = {e.id for e in warranty_alerts(s, today)}
        assert past.id in ids
        assert soon.id in ids
        assert exact.id in ids
        assert far.id not in ids
        assert none_end.id not in ids


def test_warranty_alerts_excludes_inactive_customer(engine):
    Base.metadata.create_all(engine)
    today = date(2026, 7, 15)
    with Session(engine) as s:
        active_eq = _pool_equipment(s, warranty_end=today + timedelta(days=10))
        inactive_eq = _pool_equipment(s, cust_active=False, warranty_end=today + timedelta(days=10))
        ids = {e.id for e in warranty_alerts(s, today)}
        assert active_eq.id in ids
        assert inactive_eq.id not in ids


# --- nightly scheduler wiring ---------------------------------------------------

def test_nightly_scheduler_includes_maintenance_rules(engine, monkeypatch, tmp_path):
    import app.config as config
    from app import mailer
    from app.backup import run_backup
    from app.main import create_app

    monkeypatch.setattr(config, "RUN_SCHEDULER", True)
    monkeypatch.setattr(config, "APP_PASSWORD", "testpass")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    app = create_app(engine=engine)
    try:
        jobs = app.state.scheduler.get_jobs()
        assert len(jobs) == 3
        # create_app registers every job through observability.logged_job, so
        # the scheduled callable is the wrapper -- unwrap to identify the target.
        funcs = {getattr(j.func, "__wrapped__", j.func) for j in jobs}
        assert run_backup in funcs
        assert run_nightly in funcs
        assert mailer.flush_job in funcs

        mail_job = next(j for j in jobs
                        if getattr(j.func, "__wrapped__", None) is mailer.flush_job)
        assert tuple(mail_job.args) == (engine,)
        assert "interval" in str(mail_job.trigger) or "0:05:00" in str(mail_job.trigger)
    finally:
        app.state.scheduler.shutdown(wait=False)

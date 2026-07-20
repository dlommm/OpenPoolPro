from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

OPEN_STATUSES = ("open", "scheduled")


def run_maintenance_rules(session: Session, today: date) -> int:
    """Create at most one Job per active MaintenanceRule per run.

    For each active rule (on an active property whose owning customer is
    also active) whose next_due is within lead_days of today: if no
    open/scheduled Job already exists for this rule, create one
    (scheduled_date=next_due, rule_id set, job_type from rule, unassigned)
    and catch next_due up — advancing it by interval_weeks repeatedly —
    until it sits far enough in the future that the rule is no longer due,
    so a single run never floods more than one job per rule regardless of
    how far in the past next_due had drifted. If a blocking open/scheduled
    Job already exists, next_due is left untouched until that job clears.

    Rules with a non-positive interval_weeks (no DB constraint prevents this)
    are treated as misconfigured and skipped entirely -- no job is created
    and next_due is left untouched -- since the catch-up loop above can
    never advance next_due into the future otherwise.

    Returns the number of jobs created.
    """
    created = 0
    rules = session.scalars(select(models.MaintenanceRule).where(
        models.MaintenanceRule.active)).all()
    for rule in rules:
        if rule.interval_weeks <= 0:
            continue  # misconfigured (no DB constraint) -- skip rather than infinite-loop
        prop = session.get(models.Property, rule.property_id)
        if prop is None or not prop.active or not prop.customer.active:
            continue
        if rule.next_due - timedelta(days=rule.lead_days) > today:
            continue  # not due yet

        existing = session.scalars(select(models.Job).where(
            models.Job.rule_id == rule.id,
            models.Job.status.in_(OPEN_STATUSES))).first()
        if existing is None:
            session.add(models.Job(
                property_id=rule.property_id,
                title=rule.title,
                description=rule.description,
                status="scheduled",
                scheduled_date=rule.next_due,
                job_type=rule.job_type,
                rule_id=rule.id,
            ))
            created += 1
            while rule.next_due - timedelta(days=rule.lead_days) <= today:
                rule.next_due += timedelta(weeks=rule.interval_weeks)
    session.commit()
    return created


def warranty_alerts(session: Session, today: date) -> list[models.Equipment]:
    """Equipment whose warranty ends within 60 days (including past due),
    joined pool -> property -> customer, excluding inactive customers."""
    cutoff = today + timedelta(days=60)
    return session.scalars(
        select(models.Equipment)
        .join(models.Pool, models.Equipment.pool_id == models.Pool.id)
        .join(models.Property, models.Pool.property_id == models.Property.id)
        .join(models.Customer, models.Property.customer_id == models.Customer.id)
        .where(models.Equipment.warranty_end.is_not(None),
               models.Equipment.warranty_end <= cutoff,
               models.Customer.active)
        .order_by(models.Equipment.warranty_end)
    ).all()


def run_nightly(engine) -> int:
    """APScheduler entry point: opens its own session, mirrors
    app.backup.run_backup's engine-based wiring, and runs the rules engine
    against business-local "today"."""
    from app.routers.today import business_today

    with Session(engine) as s:
        return run_maintenance_rules(s, business_today())

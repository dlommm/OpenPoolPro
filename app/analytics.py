"""Pure aggregation/query functions for the admin analytics screen.

All range-bounded functions take (session, start: date, end: date) and are
business-local-date bounded: visits by `Visit.date`, jobs by the business-local
calendar date of `Job.completed_at` (reusing `qbo._local_date`). No FastAPI
here — this module is a plain query layer.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.qbo import _local_date
from app.rates import effective_rate_cents


@dataclass
class WeekRow:
    iso_year: int
    iso_week: int
    week_start_date: date
    labor_cents: int = 0
    chem_cost_cents: int = 0
    chem_billable_cents: int = 0
    job_cents: int = 0
    total_cents: int = 0


@dataclass
class CustomerRow:
    customer: "models.Customer"
    labor_cents: int = 0
    chem_billable_cents: int = 0
    job_cents: int = 0
    total_cents: int = 0
    visit_count: int = 0


@dataclass
class TechRow:
    user: "models.User | None"
    total_cents: int = 0
    visit_count: int = 0
    job_count: int = 0
    total_minutes: int = 0
    revenue_per_hour_cents: int | None = None


@dataclass
class TypeRow:
    label: str
    total_cents: int = 0


@dataclass
class ChemRow:
    chemical: "models.Chemical"
    quantity: float = 0.0
    cost_cents: int = 0
    billable_cents: int = 0
    margin_cents: int = 0


@dataclass
class VolumeRow:
    iso_year: int
    iso_week: int
    week_start_date: date
    done: int = 0
    skipped: int = 0
    avg_minutes: int | None = None


@dataclass
class RateReviewRow:
    property: "models.Property"
    current_cents: int
    tier_cents: int
    delta_cents: int


def _markup(session) -> float:
    try:
        m = float(models.get_setting(session, "chem_markup_percent", "0"))
    except ValueError:
        return 0.0
    return m if m == m and m not in (float("inf"), float("-inf")) else 0.0


def _billable(cost_cents: int, markup: float) -> int:
    return round(cost_cents * (1 + markup / 100))


def _done_visits(session, start, end):
    return session.scalars(select(models.Visit).where(
        models.Visit.status == "done",
        models.Visit.date >= start, models.Visit.date <= end)).all()


def _all_visits(session, start, end):
    return session.scalars(select(models.Visit).where(
        models.Visit.date >= start, models.Visit.date <= end)).all()


def _done_jobs(session, start, end):
    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "done", models.Job.completed_at.is_not(None),
        models.Job.completed_at >= datetime.combine(start - timedelta(days=1), time.min),
        models.Job.completed_at <= datetime.combine(end + timedelta(days=1), time.max))).all()
    return [j for j in jobs if start <= _local_date(j.completed_at) <= end]


def _week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _tier_for_gallons(session, gallons: int) -> int:
    """Mirrors the tier ordering in app.rates.effective_rate_cents (max_gallons NULLs last)."""
    tiers = session.scalars(
        select(models.RateTier).order_by(models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)
    ).all()
    for t in tiers:
        if t.max_gallons is None or gallons <= t.max_gallons:
            return t.rate_cents
    return 0


def income_by_week(session: Session, start: date, end: date) -> list[WeekRow]:
    markup = _markup(session)
    rows: dict[tuple[int, int], WeekRow] = {}

    def row(key: tuple[int, int]) -> WeekRow:
        if key not in rows:
            y, w = key
            rows[key] = WeekRow(iso_year=y, iso_week=w, week_start_date=date.fromisocalendar(y, w, 1))
        return rows[key]

    for v in _done_visits(session, start, end):
        r = row(_week_key(v.date))
        r.labor_cents += v.rate_cents or 0
        r.chem_cost_cents += sum(vc.total_cost_cents for vc in v.chemicals)

    for j in _done_jobs(session, start, end):
        r = row(_week_key(_local_date(j.completed_at)))
        r.job_cents += j.labor_cents

    out = list(rows.values())
    for r in out:
        r.chem_billable_cents = _billable(r.chem_cost_cents, markup)
        r.total_cents = r.labor_cents + r.chem_billable_cents + r.job_cents
    out.sort(key=lambda r: (r.iso_year, r.iso_week))
    return out


def revenue_by_customer(session: Session, start: date, end: date) -> list[CustomerRow]:
    markup = _markup(session)
    rows: dict[int, CustomerRow] = {}
    chem_cost: dict[int, int] = defaultdict(int)

    def row(cust: "models.Customer") -> CustomerRow:
        return rows.setdefault(cust.id, CustomerRow(customer=cust))

    for v in _done_visits(session, start, end):
        cust = v.property.customer
        r = row(cust)
        r.labor_cents += v.rate_cents or 0
        r.visit_count += 1
        chem_cost[cust.id] += sum(vc.total_cost_cents for vc in v.chemicals)

    for j in _done_jobs(session, start, end):
        r = row(j.property.customer)
        r.job_cents += j.labor_cents

    for cid, r in rows.items():
        r.chem_billable_cents = _billable(chem_cost.get(cid, 0), markup)
        r.total_cents = r.labor_cents + r.chem_billable_cents + r.job_cents

    return sorted(rows.values(), key=lambda r: r.total_cents, reverse=True)


def revenue_by_tech(session: Session, start: date, end: date) -> list[TechRow]:
    markup = _markup(session)
    rows: dict[int | None, TechRow] = {}
    chem_cost: dict[int | None, int] = defaultdict(int)

    def row(user: "models.User | None") -> TechRow:
        key = user.id if user is not None else None
        return rows.setdefault(key, TechRow(user=user))

    for v in _done_visits(session, start, end):
        key = v.completed_by_id
        r = row(v.completed_by)
        r.total_cents += v.rate_cents or 0
        r.visit_count += 1
        chem_cost[key] += sum(vc.total_cost_cents for vc in v.chemicals)
        dur = v.duration_minutes
        if dur is not None:
            r.total_minutes += dur

    for j in _done_jobs(session, start, end):
        key = j.completed_by_id
        r = row(j.completed_by)
        r.total_cents += j.labor_cents
        r.job_count += 1
        dur = j.duration_minutes
        if dur is not None:
            r.total_minutes += dur

    for key, r in rows.items():
        r.total_cents += _billable(chem_cost.get(key, 0), markup)
        r.revenue_per_hour_cents = (
            round(r.total_cents / (r.total_minutes / 60)) if r.total_minutes > 0 else None
        )

    return sorted(rows.values(), key=lambda r: r.total_cents, reverse=True)


def revenue_by_job_type(session: Session, start: date, end: date) -> list[TypeRow]:
    markup = _markup(session)
    visits = _done_visits(session, start, end)
    visit_labor = sum(v.rate_cents or 0 for v in visits)
    visit_chem_cost = sum(vc.total_cost_cents for v in visits for vc in v.chemicals)
    visits_total = visit_labor + _billable(visit_chem_cost, markup)

    jobs = _done_jobs(session, start, end)
    repairs_total = sum(j.labor_cents for j in jobs if j.job_type == "repair")
    service_total = sum(j.labor_cents for j in jobs if j.job_type == "service")

    return [
        TypeRow(label="Visits", total_cents=visits_total),
        TypeRow(label="Repairs", total_cents=repairs_total),
        TypeRow(label="Service jobs", total_cents=service_total),
    ]


def chem_margin(session: Session, start: date, end: date) -> list[ChemRow]:
    markup = _markup(session)
    visit_ids = select(models.Visit.id).where(
        models.Visit.status == "done",
        models.Visit.date >= start, models.Visit.date <= end)
    vcs = session.scalars(select(models.VisitChemical).where(
        models.VisitChemical.visit_id.in_(visit_ids))).all()

    rows: dict[int, ChemRow] = {}
    for vc in vcs:
        r = rows.setdefault(vc.chemical_id, ChemRow(chemical=vc.chemical))
        r.quantity += vc.amount
        r.cost_cents += vc.total_cost_cents

    for r in rows.values():
        r.billable_cents = _billable(r.cost_cents, markup)
        r.margin_cents = r.billable_cents - r.cost_cents

    return sorted(rows.values(), key=lambda r: r.chemical.name)


def visit_volume(session: Session, start: date, end: date) -> list[VolumeRow]:
    rows: dict[tuple[int, int], VolumeRow] = {}
    durations: dict[tuple[int, int], list[int]] = defaultdict(list)

    def row(key: tuple[int, int]) -> VolumeRow:
        if key not in rows:
            y, w = key
            rows[key] = VolumeRow(iso_year=y, iso_week=w, week_start_date=date.fromisocalendar(y, w, 1))
        return rows[key]

    for v in _all_visits(session, start, end):
        key = _week_key(v.date)
        r = row(key)
        if v.status == "done":
            r.done += 1
            dur = v.duration_minutes
            if dur is not None:
                durations[key].append(dur)
        elif v.status == "skipped":
            r.skipped += 1

    for key, r in rows.items():
        durs = durations.get(key, [])
        r.avg_minutes = round(sum(durs) / len(durs)) if durs else None

    return sorted(rows.values(), key=lambda r: (r.iso_year, r.iso_week))


def rate_review(session: Session) -> list[RateReviewRow]:
    out: list[RateReviewRow] = []
    props = session.scalars(
        select(models.Property).where(models.Property.active.is_(True))
    ).all()
    for p in props:
        if not p.customer.active:
            continue
        active_pools = [pl for pl in p.pools if pl.active]
        if not active_pools:
            continue
        largest = max(pl.gallons for pl in active_pools)
        tier_cents = _tier_for_gallons(session, largest)
        current_cents = effective_rate_cents(session, p)
        if current_cents < tier_cents:
            out.append(RateReviewRow(property=p, current_cents=current_cents,
                                      tier_cents=tier_cents, delta_cents=tier_cents - current_cents))
    return out

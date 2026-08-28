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
from sqlalchemy.orm import Session, selectinload

from app import models, qbo, tax
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
    profit_cents: int = 0  # revenue minus chemical cost (what you actually spent)


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


# Eager-load the relationships every revenue aggregation walks (chemical lines +
# their catalog row, property→customer, the completing tech) so a month of
# visits is a handful of queries instead of one-per-visit N+1.
_VISIT_LOADS = (
    selectinload(models.Visit.chemicals).selectinload(models.VisitChemical.chemical),
    selectinload(models.Visit.property).selectinload(models.Property.customer),
    selectinload(models.Visit.completed_by),
)
_JOB_LOADS = (
    selectinload(models.Job.property).selectinload(models.Property.customer),
    selectinload(models.Job.completed_by),
)


def _done_visits(session, start, end):
    return session.scalars(select(models.Visit).where(
        models.Visit.status == "done",
        models.Visit.date >= start, models.Visit.date <= end).options(*_VISIT_LOADS)).all()


def _all_visits(session, start, end):
    return session.scalars(select(models.Visit).where(
        models.Visit.date >= start, models.Visit.date <= end).options(*_VISIT_LOADS)).all()


def _done_jobs(session, start, end):
    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "done", models.Job.completed_at.is_not(None),
        models.Job.completed_at >= datetime.combine(start - timedelta(days=1), time.min),
        models.Job.completed_at <= datetime.combine(end + timedelta(days=1), time.max))
        .options(*_JOB_LOADS)).all()
    return [j for j in jobs if start <= _local_date(j.completed_at) <= end]


@dataclass
class PeriodData:
    """One materialization of the range's rows, shared by every aggregation.

    period_summary alone runs three aggregations, the page adds the active
    tab's, and the export runs all seven — each used to re-fetch (and
    re-hydrate, with triple selectinload) the very same visit/job window.
    Fetch once via load_period and pass it in; every aggregation still accepts
    (session, start, end) alone and fetches for itself, so direct calls and
    existing tests are unaffected.
    """
    all_visits: list
    done_visits: list
    done_jobs: list


def load_period(session: Session, start: date, end: date) -> PeriodData:
    all_visits = _all_visits(session, start, end)
    return PeriodData(all_visits=all_visits,
                      done_visits=[v for v in all_visits if v.status == "done"],
                      done_jobs=_done_jobs(session, start, end))


def _data(session, start, end, data: "PeriodData | None") -> "PeriodData":
    return data if data is not None else load_period(session, start, end)


def _week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _pool_tiers(session) -> list["models.RateTier"]:
    """Pool tiers in resolution order — mirrors app.rates.effective_rate_cents
    (max_gallons NULLs last). Fetched once per rate_review call, not once per
    property."""
    return session.scalars(
        select(models.RateTier).where(models.RateTier.kind == "pool").order_by(
            models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)
    ).all()


def income_by_week(session: Session, start: date, end: date,
                   data: "PeriodData | None" = None) -> list[WeekRow]:
    data = _data(session, start, end, data)
    markup = _markup(session)
    rows: dict[tuple[int, int], WeekRow] = {}

    def row(key: tuple[int, int]) -> WeekRow:
        if key not in rows:
            y, w = key
            rows[key] = WeekRow(iso_year=y, iso_week=w, week_start_date=date.fromisocalendar(y, w, 1))
        return rows[key]

    for v in data.done_visits:
        r = row(_week_key(v.date))
        r.labor_cents += tax.effective_labor_cents(v)
        for vc in v.chemicals:
            r.chem_cost_cents += vc.total_cost_cents
            r.chem_billable_cents += qbo.billable_for_vc(vc, markup)

    for j in data.done_jobs:
        r = row(_week_key(_local_date(j.completed_at)))
        r.job_cents += tax.effective_labor_cents(j)

    out = list(rows.values())
    for r in out:
        r.total_cents = r.labor_cents + r.chem_billable_cents + r.job_cents
        r.profit_cents = r.total_cents - r.chem_cost_cents  # take-home after chemical spend
    out.sort(key=lambda r: (r.iso_year, r.iso_week))
    return out


def revenue_by_customer(session: Session, start: date, end: date,
                        data: "PeriodData | None" = None) -> list[CustomerRow]:
    data = _data(session, start, end, data)
    markup = _markup(session)
    rows: dict[int, CustomerRow] = {}
    chem_billable: dict[int, int] = defaultdict(int)

    def row(cust: "models.Customer") -> CustomerRow:
        return rows.setdefault(cust.id, CustomerRow(customer=cust))

    for v in data.done_visits:
        cust = v.property.customer
        r = row(cust)
        r.labor_cents += tax.effective_labor_cents(v)
        r.visit_count += 1
        for vc in v.chemicals:
            chem_billable[cust.id] += qbo.billable_for_vc(vc, markup)

    for j in data.done_jobs:
        r = row(j.property.customer)
        r.job_cents += tax.effective_labor_cents(j)

    for cid, r in rows.items():
        r.chem_billable_cents = chem_billable.get(cid, 0)
        r.total_cents = r.labor_cents + r.chem_billable_cents + r.job_cents

    return sorted(rows.values(), key=lambda r: r.total_cents, reverse=True)


def lifetime_revenue_cents(session: Session, customer_id: int) -> int:
    """All-time billable total for ONE customer.

    Applies exactly the rules `revenue_by_customer` does — frozen labor via
    `tax.effective_labor_cents`, chemical lines through `qbo.billable_for_vc`
    at the current markup — but scoped to this customer's properties, so the
    two agree to the cent for any customer.

    The customer-detail page previously got this number by calling
    `revenue_by_customer(session, date(2000,1,1), today)` and discarding every
    row but one. That materialized every done visit and job in the business
    (plus chemical lines, catalog rows, properties, customers and techs) on
    each page view, making the most-clicked drill-down the only page whose
    cost grew with total lifetime history rather than a bounded window.
    """
    property_ids = session.scalars(select(models.Property.id).where(
        models.Property.customer_id == customer_id)).all()
    if not property_ids:
        return 0

    markup = _markup(session)
    total = 0

    visits = session.scalars(select(models.Visit).where(
        models.Visit.property_id.in_(property_ids),
        models.Visit.status == "done").options(
            selectinload(models.Visit.chemicals))).all()
    for v in visits:
        total += tax.effective_labor_cents(v)
        for vc in v.chemicals:
            total += qbo.billable_for_vc(vc, markup)

    jobs = session.scalars(select(models.Job).where(
        models.Job.property_id.in_(property_ids),
        models.Job.status == "done",
        models.Job.completed_at.is_not(None))).all()
    for j in jobs:
        total += tax.effective_labor_cents(j)

    return total


def revenue_by_tech(session: Session, start: date, end: date,
                    data: "PeriodData | None" = None) -> list[TechRow]:
    data = _data(session, start, end, data)
    markup = _markup(session)
    rows: dict[int | None, TechRow] = {}
    chem_billable: dict[int | None, int] = defaultdict(int)

    def row(user: "models.User | None") -> TechRow:
        key = user.id if user is not None else None
        return rows.setdefault(key, TechRow(user=user))

    for v in data.done_visits:
        key = v.completed_by_id
        r = row(v.completed_by)
        r.total_cents += tax.effective_labor_cents(v)
        r.visit_count += 1
        for vc in v.chemicals:
            chem_billable[key] += qbo.billable_for_vc(vc, markup)
        dur = v.duration_minutes
        if dur is not None:
            r.total_minutes += dur

    for j in data.done_jobs:
        key = j.completed_by_id
        r = row(j.completed_by)
        r.total_cents += tax.effective_labor_cents(j)
        r.job_count += 1
        dur = j.duration_minutes
        if dur is not None:
            r.total_minutes += dur

    for key, r in rows.items():
        r.total_cents += chem_billable.get(key, 0)
        r.revenue_per_hour_cents = (
            round(r.total_cents / (r.total_minutes / 60)) if r.total_minutes > 0 else None
        )

    return sorted(rows.values(), key=lambda r: r.total_cents, reverse=True)


def revenue_by_job_type(session: Session, start: date, end: date,
                        data: "PeriodData | None" = None) -> list[TypeRow]:
    data = _data(session, start, end, data)
    markup = _markup(session)
    visits = data.done_visits
    visit_labor = sum(tax.effective_labor_cents(v) for v in visits)
    visit_chem_billable = sum(qbo.billable_for_vc(vc, markup)
                               for v in visits for vc in v.chemicals)
    visits_total = visit_labor + visit_chem_billable

    jobs = data.done_jobs
    repairs_total = sum(tax.effective_labor_cents(j) for j in jobs if j.job_type == "repair")
    service_total = sum(tax.effective_labor_cents(j) for j in jobs if j.job_type == "service")

    return [
        TypeRow(label="Visits", total_cents=visits_total),
        TypeRow(label="Repairs", total_cents=repairs_total),
        TypeRow(label="Service jobs", total_cents=service_total),
    ]


def chem_margin(session: Session, start: date, end: date,
                data: "PeriodData | None" = None) -> list[ChemRow]:
    data = _data(session, start, end, data)
    markup = _markup(session)

    rows: dict[int, ChemRow] = {}
    for vc in (vc for v in data.done_visits for vc in v.chemicals):
        r = rows.setdefault(vc.chemical_id, ChemRow(chemical=vc.chemical))
        r.quantity += vc.amount
        r.cost_cents += vc.total_cost_cents
        r.billable_cents += qbo.billable_for_vc(vc, markup)

    for r in rows.values():
        r.margin_cents = r.billable_cents - r.cost_cents

    return sorted(rows.values(), key=lambda r: r.chemical.name)


def visit_volume(session: Session, start: date, end: date,
                 data: "PeriodData | None" = None) -> list[VolumeRow]:
    data = _data(session, start, end, data)
    rows: dict[tuple[int, int], VolumeRow] = {}
    durations: dict[tuple[int, int], list[int]] = defaultdict(list)

    def row(key: tuple[int, int]) -> VolumeRow:
        if key not in rows:
            y, w = key
            rows[key] = VolumeRow(iso_year=y, iso_week=w, week_start_date=date.fromisocalendar(y, w, 1))
        return rows[key]

    for v in data.all_visits:
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


def period_summary(session: Session, start: date, end: date,
                   data: "PeriodData | None" = None) -> dict:
    """Headline KPIs for the selected range, shown as tiles above the tabs.
    Reuses the same aggregations as the tabs so every number reconciles."""
    data = _data(session, start, end, data)
    weeks = income_by_week(session, start, end, data)
    revenue = sum(w.total_cents for w in weeks)
    profit = sum(w.profit_cents for w in weeks)
    chem_cost = sum(w.chem_cost_cents for w in weeks)

    volume = visit_volume(session, start, end, data)
    visits_done = sum(v.done for v in volume)

    customers = revenue_by_customer(session, start, end, data)
    top = customers[0] if customers else None

    return {
        "revenue_cents": revenue,
        "profit_cents": profit,
        "chem_cost_cents": chem_cost,
        "visits_done": visits_done,
        "avg_per_visit_cents": round(revenue / visits_done) if visits_done else 0,
        "customer_count": len(customers),
        "top_customer": top.customer.name if top else None,
        "top_customer_cents": top.total_cents if top else 0,
    }


def rate_review(session: Session) -> list[RateReviewRow]:
    out: list[RateReviewRow] = []
    # Every property walks .customer and .pools — eager-load both, and resolve
    # the tier ladder once, or this loop lazy-loads ~4 queries per property.
    # (effective_rate_cents still queries the tiny tier table per property for
    # its own resolution; the relationship N+1 was the real cost here.)
    props = session.scalars(
        select(models.Property).where(models.Property.active.is_(True))
        .options(selectinload(models.Property.customer),
                 selectinload(models.Property.pools))
    ).all()
    tiers = _pool_tiers(session)
    for p in props:
        if not p.customer.active:
            continue
        active_pools = [pl for pl in p.pools if pl.active]
        if not active_pools:
            continue
        pool_pools = [pl for pl in active_pools if pl.kind == "pool"]
        if not pool_pools:
            continue
        largest = max(pl.gallons for pl in pool_pools)
        tier_cents = next((t.rate_cents for t in tiers
                           if t.max_gallons is None or largest <= t.max_gallons), 0)
        current_cents = effective_rate_cents(session, p)
        if current_cents < tier_cents:
            out.append(RateReviewRow(property=p, current_cents=current_cents,
                                      tier_cents=tier_cents, delta_cents=tier_cents - current_cents))
    return out

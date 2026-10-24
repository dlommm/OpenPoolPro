"""The Analytics overview: one range (3, 6 or 12 months) scoping every panel.

Pure query/aggregation layer, like app.analytics, whose money rules it reuses
to the cent: service is the frozen visit labor (`tax.effective_labor_cents`),
chemicals are billed through `qbo.billable_for_vc` at the current markup, and
"repairs and jobs" is the labor of finished jobs dated by their business-local
completion day.

The current month is always partial. It is drawn and totalled, but the
like-for-like comparison uses full months only: the full months of the window
against the same number of months just before it.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import analytics, models, qbo, receivables, tax
from app.qbo import _local_date
from app.rates import effective_rate_cents, largest_pool_gallons
from app.timefmt import business_today, local_date

RANGES = (3, 6, 12)
DEFAULT_RANGE = 6
REVIEW_SHARE = 0.15  # chemical cost at or above this share of the rate -> review

# Visits a week each schedule frequency produces, for "recurring weekly service".
WEEKLY_FACTOR = {"weekly": 1.0, "biweekly": 0.5, "every4weeks": 0.25}

SOURCES = (
    ("service", "Weekly service", "var(--c1)"),
    ("chemicals", "Chemicals", "var(--c2)"),
    ("jobs", "Repairs and jobs", "var(--c3)"),
)

AGING_LABELS = {
    "Current": "Not due yet",
    "1-30": "1–30 days late",
    "31-60": "31–60 days late",
    "61-90": "61–90 days late",
    "90+": "Over 90 days",
}

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_range(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return n if n in RANGES else DEFAULT_RANGE


def month_keys(today: date, months: int) -> list[tuple[int, int]]:
    """The `months` calendar months ending with today's, oldest first."""
    out = []
    y, m = today.year, today.month
    for _ in range(max(1, months)):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _shift(key: tuple[int, int], back: int) -> tuple[int, int]:
    idx = key[0] * 12 + (key[1] - 1) - back
    return (idx // 12, idx % 12 + 1)


@dataclass
class MonthRow:
    year: int
    month: int
    service_cents: int = 0
    chem_cents: int = 0
    jobs_cents: int = 0
    visits: int = 0
    partial_through: int | None = None  # day of month the data runs to, when partial

    @property
    def key(self) -> tuple[int, int]:
        return (self.year, self.month)

    @property
    def total_cents(self) -> int:
        return self.service_cents + self.chem_cents + self.jobs_cents

    @property
    def partial(self) -> bool:
        return self.partial_through is not None

    @property
    def short(self) -> str:
        return MONTH_NAMES[self.month - 1][:3]

    @property
    def axis_label(self) -> str:
        if self.partial:
            return f"{self.short} (to {ordinal(self.partial_through)})"
        return self.short

    @property
    def long(self) -> str:
        name = MONTH_NAMES[self.month - 1]
        if self.partial:
            return f"{name} 1–{self.partial_through}, {self.year} (so far)"
        return f"{name} {self.year}"

    def part(self, source: str) -> int:
        return {"service": self.service_cents, "chemicals": self.chem_cents,
                "jobs": self.jobs_cents}[source]


def monthly_revenue(session: Session, keys: list[tuple[int, int]], today: date,
                    data: "analytics.PeriodData | None" = None) -> list[MonthRow]:
    """One row per month key (zeros included), split service/chemicals/jobs."""
    if not keys:
        return []
    start = date(keys[0][0], keys[0][1], 1)
    if data is None:
        data = analytics.load_period(session, start, today)
    markup = analytics._markup(session)
    rows = {k: MonthRow(year=k[0], month=k[1]) for k in keys}
    cur = (today.year, today.month)
    if cur in rows:
        rows[cur].partial_through = today.day
    for v in data.done_visits:
        r = rows.get((v.date.year, v.date.month))
        if r is None:
            continue
        r.visits += 1
        r.service_cents += tax.effective_labor_cents(v)
        r.chem_cents += sum(qbo.billable_for_vc(vc, markup) for vc in v.chemicals)
    for j in data.done_jobs:
        d = _local_date(j.completed_at)
        r = rows.get((d.year, d.month))
        if r is not None:
            r.jobs_cents += tax.effective_labor_cents(j)
    return [rows[k] for k in keys]


@dataclass
class Delta:
    pct: float
    current_cents: int
    previous_cents: int
    current_span: str
    previous_span: str

    @property
    def up(self) -> bool:
        return self.pct >= 0


def _span(rows: list[MonthRow]) -> str:
    if not rows:
        return ""
    a, b = rows[0], rows[-1]
    if a.key == b.key:
        return f"{a.short} {a.year}"
    if a.year == b.year:
        return f"{a.short}–{b.short} {b.year}"
    return f"{a.short} {a.year}–{b.short} {b.year}"


def like_for_like(window: list[MonthRow], before: list[MonthRow]) -> Delta | None:
    """Full months of the window against the same count of months before it.

    None when there is nothing fair to compare: no full month in the window
    (a one-month range is just the partial current month), or no revenue at
    all in the earlier months (a new business, or history not entered).
    """
    full = [m for m in window if not m.partial]
    if not full:
        return None
    prev = before[-len(full):] if len(before) >= len(full) else []
    prev_total = sum(m.total_cents for m in prev)
    if not prev or prev_total <= 0:
        return None
    cur_total = sum(m.total_cents for m in full)
    return Delta(pct=(cur_total / prev_total - 1) * 100, current_cents=cur_total,
                 previous_cents=prev_total, current_span=_span(full),
                 previous_span=_span(prev))


@dataclass
class MixRow:
    key: str
    label: str
    colour: str
    cents: int
    share: float  # 0..1


def income_mix(months: list[MonthRow]) -> list[MixRow]:
    total = sum(m.total_cents for m in months)
    out = []
    for key, label, colour in SOURCES:
        cents = sum(m.part(key) for m in months)
        out.append(MixRow(key, label, colour, cents, cents / total if total else 0.0))
    return out


@dataclass
class RouteRow:
    name: str
    tech: "models.User | None"
    customers: int = 0
    weekly_cents: int = 0

    @property
    def unassigned(self) -> bool:
        return self.tech is None


def _service_props(session: Session) -> list["models.Property"]:
    return session.scalars(
        select(models.Property).join(models.Customer).where(
            models.Property.active.is_(True), models.Customer.active.is_(True))
        .options(selectinload(models.Property.customer),
                 selectinload(models.Property.pools),
                 selectinload(models.Property.assigned_tech))).all()


def recurring_by_route(session: Session) -> list[RouteRow]:
    """Recurring service a week on each technician's route, from today's customer list.

    Biweekly and four-weekly properties count at a half and a quarter of their
    rate, so every route is in the same per-week unit. Unassigned recurring
    properties are their own row, last, so nothing billable goes unseen.
    """
    rows: dict[int | None, RouteRow] = {}
    seen: dict[int | None, set[int]] = defaultdict(set)
    for p in _service_props(session):
        factor = WEEKLY_FACTOR.get(p.frequency)
        if not factor:
            continue
        key = p.assigned_tech_id
        row = rows.get(key)
        if row is None:
            row = rows[key] = RouteRow(
                name=p.assigned_tech.name if p.assigned_tech else "Unassigned", tech=p.assigned_tech)
        row.weekly_cents += round(effective_rate_cents(session, p) * factor)
        seen[key].add(p.customer_id)
    for key, row in rows.items():
        row.customers = len(seen[key])
    return sorted(rows.values(), key=lambda r: (r.unassigned, -r.weekly_cents, r.name))


@dataclass
class AgingRow:
    label: str
    cents: int = 0
    count: int = 0
    level: str = ""  # "" | "warn" | "neg"


def receivables_by_age(session: Session, today: date) -> list[AgingRow]:
    """Open balances in the accountant's buckets, from the invoices themselves."""
    order = [label for label, _ in receivables.AGING_BUCKETS]
    rows = {label: AgingRow(label=AGING_LABELS.get(label, label)) for label in order}
    for inv in receivables.open_invoices(session):
        r = rows[receivables.bucket_for(inv.days_overdue(today))]
        r.cents += inv.balance_cents
        r.count += 1
    out = [rows[label] for label in order]
    for i, r in enumerate(out):
        r.level = "" if i == 0 else ("warn" if i == 1 else "neg")
    return out


@dataclass
class RatePoint:
    name: str
    property_id: int
    customer_id: int
    rate_cents: int
    chem_per_visit_cents: int
    visits: int
    gallons: int
    avg_minutes: int | None

    @property
    def share(self) -> float:
        return self.chem_per_visit_cents / self.rate_cents if self.rate_cents else 0.0

    @property
    def flag(self) -> bool:
        return self.share >= REVIEW_SHARE


def rate_points(session: Session, data: "analytics.PeriodData",
                start: date, end: date) -> list[RatePoint]:
    """Chemical cost per visit as a share of the rate, per active property.

    Chemical COST (what the business spends), not the billed amount: the
    question is whether the rate still covers the chemicals. The rate is the
    property's current rate (the one a review would change), falling back to
    what its visits in the range were billed when no rate resolves.
    """
    per: dict[int, list] = defaultdict(list)
    for v in data.done_visits:
        if start <= v.date <= end:
            per[v.property_id].append(v)
    if not per:
        return []
    props = session.scalars(
        select(models.Property).where(models.Property.id.in_(per))
        .options(selectinload(models.Property.customer),
                 selectinload(models.Property.pools))).all()
    multi = defaultdict(int)
    for p in props:
        multi[p.customer_id] += 1
    out = []
    for p in props:
        if not p.active or not p.customer.active:
            continue
        visits = per[p.id]
        n = len(visits)
        chem = sum(vc.total_cost_cents for v in visits for vc in v.chemicals)
        rate = effective_rate_cents(session, p) or round(
            sum(tax.effective_labor_cents(v) for v in visits) / n)
        if rate <= 0:
            continue
        mins = [v.duration_minutes for v in visits if v.duration_minutes is not None]
        name = p.customer.name if multi[p.customer_id] == 1 else f"{p.customer.name} · {p.label}"
        out.append(RatePoint(
            name=name, property_id=p.id, customer_id=p.customer_id, rate_cents=rate,
            chem_per_visit_cents=round(chem / n), visits=n,
            gallons=largest_pool_gallons(p) or largest_pool_gallons(p, "spa"),
            avg_minutes=round(sum(mins) / len(mins)) if mins else None))
    return sorted(out, key=lambda r: (-r.share, r.name))


@dataclass
class Move:
    name: str
    customer_id: int
    kind: str  # "gained" | "lost"
    on: date


@dataclass
class Movement:
    gained: list[Move] = field(default_factory=list)
    lost: list[Move] = field(default_factory=list)
    by_month: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


def customer_movement(session: Session, keys: list[tuple[int, int]], today: date) -> Movement:
    """Customers gained (by when they were added) and lost, per month of the window.

    A lost customer is one marked inactive, dated by when that happened.
    Customers deactivated before that was recorded fall back to their last
    completed visit, then to when they were added.
    """
    mv = Movement(by_month={k: (0, 0) for k in keys})
    if not keys:
        return mv
    start = date(keys[0][0], keys[0][1], 1)
    customers = session.scalars(select(models.Customer)).all()
    last_visit = dict(session.execute(
        select(models.Property.customer_id, func.max(models.Visit.date))
        .join(models.Visit, models.Visit.property_id == models.Property.id)
        .join(models.Customer, models.Customer.id == models.Property.customer_id)
        .where(models.Visit.status == "done", models.Customer.active.is_(False))
        .group_by(models.Property.customer_id)).all())

    def add(move: Move, slot: int):
        k = (move.on.year, move.on.month)
        if k in mv.by_month and start <= move.on <= today:
            g, lost = mv.by_month[k]
            mv.by_month[k] = (g + (slot == 0), lost + (slot == 1))
            (mv.gained if slot == 0 else mv.lost).append(move)

    for c in customers:
        added = local_date(c.created_at) if c.created_at else None
        if added:
            add(Move(c.name, c.id, "gained", added), 0)
        if not c.active:
            ended = (local_date(c.deactivated_at) if c.deactivated_at else None) \
                or last_visit.get(c.id) or added
            if ended:
                add(Move(c.name, c.id, "lost", ended), 1)
    mv.gained.sort(key=lambda m: (m.on, m.name), reverse=True)
    mv.lost.sort(key=lambda m: (m.on, m.name), reverse=True)
    return mv


def nice_top(value: float, steps: int = 3) -> int:
    """A round axis maximum at or above `value`, divisible into `steps` ticks."""
    if value <= 0:
        return steps
    raw = value / steps
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            return int(round(step * steps))
    return int(round(10 * mag * steps))


def nice_range(lo: float, hi: float, steps: int = 4) -> tuple[int, int]:
    """(start, step) of a round axis covering lo..hi in `steps` equal steps.

    The rate axis starts near the cheapest rate rather than at zero, so a
    business whose rates all sit between $60 and $90 gets a readable spread.
    """
    lo, hi = max(0, lo), max(0, hi)
    if hi <= lo:
        lo, hi = max(0, lo - steps), hi + steps
    raw = (hi - lo) / steps
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        step = int(m * mag)
        start = int(lo // step * step)
        if start + step * steps >= hi:
            return start, step
    return 0, int(hi // steps + 1)


@dataclass
class Overview:
    months_n: int
    today: date
    months: list[MonthRow]
    delta: Delta | None
    mix: list[MixRow]
    routes: list[RouteRow]
    aging: list[AgingRow]
    rates: list[RatePoint]
    movement: Movement
    active_customers: int

    @property
    def start(self) -> date:
        return date(self.months[0].year, self.months[0].month, 1)

    @property
    def revenue_cents(self) -> int:
        return sum(m.total_cents for m in self.months)

    @property
    def visits(self) -> int:
        return sum(m.visits for m in self.months)

    @property
    def per_visit_cents(self) -> int:
        return round(self.revenue_cents / self.visits) if self.visits else 0

    @property
    def full_months(self) -> list[MonthRow]:
        return [m for m in self.months if not m.partial]

    @property
    def visits_per_full_month(self) -> int | None:
        full = self.full_months
        return round(sum(m.visits for m in full) / len(full)) if full else None

    @property
    def best_full_month(self) -> MonthRow | None:
        full = [m for m in self.full_months if m.total_cents > 0]
        return max(full, key=lambda m: m.total_cents) if full else None

    @property
    def span(self) -> str:
        return _span(self.months)

    @property
    def flagged(self) -> list[RatePoint]:
        return [p for p in self.rates if p.flag]

    @property
    def weekly_recurring_cents(self) -> int:
        return sum(r.weekly_cents for r in self.routes)

    @property
    def ar_total_cents(self) -> int:
        return sum(a.cents for a in self.aging)

    @property
    def ar_over_30_cents(self) -> int:
        return sum(a.cents for a in self.aging[2:])

    @property
    def has_revenue(self) -> bool:
        return self.revenue_cents > 0


def build(session: Session, months: int = DEFAULT_RANGE, today: date | None = None) -> Overview:
    """Everything the overview shows, for the `months` months ending today."""
    today = today or business_today()
    keys = month_keys(today, months)
    full_count = len(keys) - 1
    before_keys = [_shift(keys[0], full_count - i) for i in range(full_count)]
    load_from = before_keys[0] if before_keys else keys[0]
    data = analytics.load_period(session, date(load_from[0], load_from[1], 1), today)
    window = monthly_revenue(session, keys, today, data)
    before = monthly_revenue(session, before_keys, today, data) if before_keys else []
    start = date(keys[0][0], keys[0][1], 1)
    active = len(session.scalars(select(models.Customer.id).where(
        models.Customer.active.is_(True))).all())
    return Overview(
        months_n=len(keys), today=today, months=window,
        delta=like_for_like(window, before), mix=income_mix(window),
        routes=recurring_by_route(session), aging=receivables_by_age(session, today),
        rates=rate_points(session, data, start, today),
        movement=customer_movement(session, keys, today), active_customers=active)

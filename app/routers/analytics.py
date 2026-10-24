import csv
import io
import math
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics, analytics_overview, charts, models
from app.db import get_session
from app.deps import require_admin
from app.qbo import csv_safe
from app.templating import templates
from app.timefmt import business_today

router = APIRouter(dependencies=[Depends(require_admin)])

TAB_LABELS = {
    "income": "Income",
    "customers": "Customers",
    "crew": "Crew",
    "types": "Job types",
    "chemicals": "Chemicals",
    "volume": "Volume",
    "rates": "Rates",
}
# The overview leads; the detailed tabs (and their exports) follow it.
VIEW_LABELS = {"overview": "Overview", **TAB_LABELS}
MAX_SPAN_DAYS = 5 * 365


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_applied(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None  # non-int: ignore banner rather than 500 or render garbage


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return start, end


def _current_month() -> tuple[date, date]:
    d = business_today()
    return _month_bounds(d.year, d.month)


def _quick_links() -> list[tuple[str, date, date]]:
    today = business_today()
    this_start, this_end = _month_bounds(today.year, today.month)
    ly, lm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    last_start, last_end = _month_bounds(ly, lm)
    year_start, year_end = date(today.year, 1, 1), date(today.year, 12, 31)
    this_monday = today - timedelta(days=today.weekday())
    w12_end = this_monday + timedelta(days=6)
    w12_start = this_monday - timedelta(weeks=11)
    return [
        ("This month", this_start, this_end),
        ("Last month", last_start, last_end),
        ("This year", year_start, year_end),
        ("Last 12 weeks", w12_start, w12_end),
    ]


def _money_axis(cents: int) -> str:
    dollars = cents / 100
    if dollars == 0:
        return "$0"
    if dollars >= 1000:
        k = dollars / 1000
        return f"${k:.1f}".rstrip("0").rstrip(".") + "k"
    return f"${dollars:,.0f}"


def _whole(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def _overview_view(ov: "analytics_overview.Overview") -> dict:
    """Chart geometry and text alternatives for the overview template.

    Every number is computed server-side; the template only places it. Marks
    are SVG (charts.py); words are HTML laid over them.
    """
    n = len(ov.months)
    cols = f"grid-template-columns:repeat({n},minmax(0,1fr))"

    # Revenue by month, stacked service / chemicals / repairs and jobs.
    top = analytics_overview.nice_top(max((m.total_cents for m in ov.months), default=0))
    stack_svg = charts.stacked_columns(
        [((m.service_cents, m.chem_cents, m.jobs_cents), m.partial) for m in ov.months], top)
    yticks = [_money_axis(round(top * f)) for f in (1, 2 / 3, 1 / 3, 0)]
    stack_aria = (f"Revenue by month, {ov.span}, split into service, chemicals and repairs. "
                  + "; ".join(f"{m.long}: {_whole(m.total_cents)}" for m in ov.months) + ".")

    spark_line, spark_area = charts.sparkline([m.total_cents for m in ov.months])

    # Rate review scatter: x = rate, y = chemical cost share of the rate.
    pts = ov.rates
    # Thirds of a multiple of 30% keep every tick on a round 10%.
    y_top = 0.30 * max(1, math.ceil(max((p.share for p in pts), default=0) / 0.30 - 1e-9))
    # x axis in whole dollars, starting near the cheapest rate.
    x0, xstep = analytics_overview.nice_range(
        min((p.rate_cents for p in pts), default=0) / 100,
        max((p.rate_cents for p in pts), default=0) / 100)
    x_span = xstep * 4 * 100  # cents
    placed = []
    labelled = 0
    for p in pts:  # highest share first, so the labelled four are the worst
        x = (p.rate_cents - x0 * 100) / x_span * 100 if x_span else 0
        y = min(p.share, y_top) / y_top * 100
        words = [w for w in p.name.split() if w.lower() != "the"] or [p.name]
        label = p.flag and labelled < 4
        labelled += label
        placed.append({"p": p, "x": x, "y": y, "left_tip": x > 60, "short": words[0],
                       "label": label})
    scatter_svg = charts.scatter_marks([(d["x"], d["y"], d["p"].flag) for d in placed],
                                       threshold=analytics_overview.REVIEW_SHARE / y_top * 100)
    y_ticks = [f"{y_top * f * 100:.0f}%" for f in (1, 2 / 3, 1 / 3, 0)]
    x_ticks = [_money_axis((x0 + xstep * k) * 100) for k in range(5)]
    flagged = ov.flagged
    rate_aria = ("Scatter of chemical cost per visit as a share of the rate, against the rate, "
                 f"for {len(pts)} properties. "
                 + (", ".join(f"{p.name} {p.share * 100:.1f}%" for p in flagged)
                    + (" is" if len(flagged) == 1 else " are") + " at or above 15 percent."
                    if flagged else "None is at or above 15 percent."))

    # Customers gained and lost.
    moves = [ov.movement.by_month.get(m.key, (0, 0)) for m in ov.months]
    mv_top = max([max(g, lost) for g, lost in moves] + [1])
    move_svg = charts.diverging_columns(moves, mv_top)
    gained, lost = len(ov.movement.gained), len(ov.movement.lost)
    move_aria = (f"Customers gained and lost by month, {ov.span}: {gained} gained, {lost} lost. "
                 + "; ".join(f"{m.long}: {g} gained, {lo} lost"
                             for m, (g, lo) in zip(ov.months, moves) if g or lo))

    route_max = max((r.weekly_cents for r in ov.routes), default=0) or 1
    aging_max = max((a.cents for a in ov.aging), default=0) or 1
    mix_total = sum(x.cents for x in ov.mix)

    cur = ov.months[-1]
    partial_note = (f"{analytics_overview.MONTH_NAMES[cur.month - 1]} runs to the "
                    f"{analytics_overview.ordinal(cur.partial_through)}, so it is shown "
                    "but left out of comparisons") if cur.partial else ""

    return {
        "partial_note": partial_note,
        "cols": cols, "stack_svg": stack_svg, "yticks": yticks, "stack_aria": stack_aria,
        "stack_tip_left": [i >= n / 2 for i in range(n)],
        "spark_line": spark_line, "spark_area": spark_area,
        "scatter_svg": scatter_svg, "scatter": placed, "y_ticks": y_ticks, "x_ticks": x_ticks,
        "threshold_pct": analytics_overview.REVIEW_SHARE / y_top * 100, "rate_aria": rate_aria,
        "move_svg": move_svg, "moves": moves, "move_aria": move_aria,
        "gained": gained, "lost": lost,
        "route_max": route_max, "aging_max": aging_max, "mix_total": mix_total,
        "mix_aria": ", ".join(f"{x.label} {x.share * 100:.1f}%" for x in ov.mix),
        "whole": _whole,
    }


def _overview_response(request: Request, session: Session, range_raw) -> object:
    months = analytics_overview.parse_range(range_raw)
    ov = analytics_overview.build(session, months)
    ctx = {
        "tab": "overview",
        "tab_labels": VIEW_LABELS,
        "range": months,
        "ranges": analytics_overview.RANGES,
        "ov": ov,
        "v": _overview_view(ov),
        "sources": analytics_overview.SOURCES,
        "start": ov.start,
        "end": ov.today,
    }
    return templates.TemplateResponse(request, "analytics.html", ctx)


@router.get("/analytics")
def analytics_page(request: Request, tab: str = "overview",
                    start: str | None = None, end: str | None = None,
                    applied: str | None = None,
                    range_: str | None = Query(None, alias="range"),
                    session: Session = Depends(get_session)):
    if tab not in TAB_LABELS:
        return _overview_response(request, session, range_)

    start_d = _parse_date(start)
    end_d = _parse_date(end)
    if start_d is None or end_d is None:
        start_d, end_d = _current_month()
    elif start_d > end_d:
        start_d, end_d = end_d, start_d

    ctx = {
        "tab": tab,
        "tab_labels": VIEW_LABELS,
        "start": start_d,
        "end": end_d,
        "quick": _quick_links(),
        "too_large": False,
        "rows": [],
        "applied": _parse_applied(applied),
    }

    if (end_d - start_d).days > MAX_SPAN_DAYS:
        ctx["too_large"] = True
        return templates.TemplateResponse(request, "analytics.html", ctx)

    # One materialization of the window, shared by the KPI tiles and the
    # active tab — period_summary alone runs three aggregations, and each used
    # to re-fetch the same rows.
    data = analytics.load_period(session, start_d, end_d)
    ctx["summary"] = analytics.period_summary(session, start_d, end_d, data)

    if tab == "income":
        rows = analytics.income_by_week(session, start_d, end_d, data)
        ctx["max_total"] = max((r.total_cents for r in rows), default=0)
    elif tab == "customers":
        rows = analytics.revenue_by_customer(session, start_d, end_d, data)
    elif tab == "crew":
        rows = analytics.revenue_by_tech(session, start_d, end_d, data)
    elif tab == "types":
        rows = analytics.revenue_by_job_type(session, start_d, end_d, data)
    elif tab == "chemicals":
        rows = analytics.chem_margin(session, start_d, end_d, data)
    elif tab == "volume":
        rows = analytics.visit_volume(session, start_d, end_d, data)
    else:  # rates
        rows = analytics.rate_review(session)

    ctx["rows"] = rows
    ctx["chart"] = _build_chart(tab, rows)
    return templates.TemplateResponse(request, "analytics.html", ctx)


def _build_chart(tab: str, rows) -> str | None:
    """Inline-SVG chart for the tab (None for the rates ops tab)."""
    if tab == "income":
        return charts.bar_chart([(f"W{r.iso_week}", r.total_cents) for r in rows],
                                kind="money", empty="No income in this range")
    if tab == "customers":
        return charts.hbar_chart([(r.customer.name, r.total_cents) for r in rows],
                                 kind="money", empty="No customer revenue in this range")
    if tab == "crew":
        return charts.hbar_chart(
            [((r.user.name if r.user else "Unattributed"), r.total_cents) for r in rows],
            kind="money", empty="No crew activity in this range")
    if tab == "types":
        slices = [(r.label, r.total_cents) for r in rows]
        donut = charts.donut_chart(slices, kind="money", empty="No job-type revenue")
        leg = charts.legend(slices, kind="money")
        return (f'<div class="flex flex-wrap items-center gap-6">'
                f'<div class="shrink-0">{donut}</div>'
                f'<div class="min-w-0 flex-1">{leg}</div></div>')
    if tab == "chemicals":
        ranked = sorted(rows, key=lambda r: r.margin_cents, reverse=True)
        return charts.hbar_chart([(r.chemical.name, r.margin_cents) for r in ranked],
                                 kind="money", empty="No chemical usage in this range")
    if tab == "volume":
        return charts.bar_chart([(f"W{r.iso_week}", r.done) for r in rows],
                                kind="int", empty="No visit activity in this range")
    return None


def _format_cents(cents: int | None) -> str:
    """Format cents as dollars with 2 decimal places, or empty string if None."""
    if cents is None:
        return ""
    return f"{cents / 100:.2f}"


@router.get("/analytics/export")
def analytics_export(request: Request, tab: str = "income",
                     start: str | None = None, end: str | None = None,
                     session: Session = Depends(get_session)):
    """Export analytics tab data as CSV."""
    if tab not in TAB_LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab}")

    start_d = _parse_date(start)
    end_d = _parse_date(end)
    if start_d is None or end_d is None:
        start_d, end_d = _current_month()
    elif start_d > end_d:
        start_d, end_d = end_d, start_d

    if (end_d - start_d).days > MAX_SPAN_DAYS:
        raise HTTPException(status_code=400, detail="Date range too large")

    rows = _fetch_rows(tab, session, start_d, end_d)

    # Generate CSV
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    if tab == "income":
        writer.writerow(["week_start", "iso_week", "labor", "chemicals_cost",
                         "chemicals_billable", "jobs", "revenue", "profit"])
        for r in rows:
            writer.writerow([
                r.week_start_date,
                r.iso_week,
                _format_cents(r.labor_cents),
                _format_cents(r.chem_cost_cents),
                _format_cents(r.chem_billable_cents),
                _format_cents(r.job_cents),
                _format_cents(r.total_cents),
                _format_cents(r.profit_cents),
            ])
    elif tab == "customers":
        writer.writerow(["customer", "visits", "labor", "chemicals", "jobs", "total"])
        for r in rows:
            writer.writerow([
                csv_safe(r.customer.name),
                r.visit_count,
                _format_cents(r.labor_cents),
                _format_cents(r.chem_billable_cents),
                _format_cents(r.job_cents),
                _format_cents(r.total_cents),
            ])
    elif tab == "crew":
        writer.writerow(["tech", "visits", "jobs", "minutes", "revenue", "revenue_per_hour"])
        for r in rows:
            tech_name = csv_safe(r.user.name if r.user else "Unattributed")
            writer.writerow([
                tech_name,
                r.visit_count,
                r.job_count,
                r.total_minutes,
                _format_cents(r.total_cents),
                _format_cents(r.revenue_per_hour_cents),
            ])
    elif tab == "types":
        writer.writerow(["type", "total"])
        for r in rows:
            writer.writerow([
                csv_safe(r.label),
                _format_cents(r.total_cents),
            ])
    elif tab == "chemicals":
        writer.writerow(["chemical", "quantity", "unit", "cost", "billable", "margin"])
        for r in rows:
            writer.writerow([
                csv_safe(r.chemical.name),
                f"{r.quantity:.2f}",
                csv_safe(r.chemical.unit),
                _format_cents(r.cost_cents),
                _format_cents(r.billable_cents),
                _format_cents(r.margin_cents),
            ])
    elif tab == "volume":
        writer.writerow(["week_start", "done", "skipped", "avg_minutes"])
        for r in rows:
            writer.writerow([
                r.week_start_date,
                r.done,
                r.skipped,
                r.avg_minutes if r.avg_minutes is not None else "",
            ])
    else:  # rates
        writer.writerow(["customer", "property", "current", "tier", "delta"])
        for r in rows:
            writer.writerow([
                csv_safe(r.property.customer.name),
                csv_safe(r.property.label),
                _format_cents(r.current_cents),
                _format_cents(r.tier_cents),
                _format_cents(r.delta_cents),
            ])

    csv_content = output.getvalue()
    models.audit(session, request.state.user.id, "export.analytics",
                 detail=f"{tab} {start_d.isoformat()}..{end_d.isoformat()}")
    session.commit()
    filename = f"analytics-{tab}-{start_d.isoformat()}-{end_d.isoformat()}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def _resolve_range(start, end):
    start_d, end_d = _parse_date(start), _parse_date(end)
    if start_d is None or end_d is None:
        start_d, end_d = _current_month()
    elif start_d > end_d:
        start_d, end_d = end_d, start_d
    return start_d, end_d


def _fetch_rows(tab: str, session, start_d, end_d, data=None):
    if tab == "rates":  # not range-bounded; skip the window fetch entirely
        return analytics.rate_review(session)
    data = data if data is not None else analytics.load_period(session, start_d, end_d)
    fns = {
        "income": lambda: analytics.income_by_week(session, start_d, end_d, data),
        "customers": lambda: analytics.revenue_by_customer(session, start_d, end_d, data),
        "crew": lambda: analytics.revenue_by_tech(session, start_d, end_d, data),
        "types": lambda: analytics.revenue_by_job_type(session, start_d, end_d, data),
        "chemicals": lambda: analytics.chem_margin(session, start_d, end_d, data),
        "volume": lambda: analytics.visit_volume(session, start_d, end_d, data),
    }
    return fns[tab]()


@router.get("/analytics/export.xlsx")
def analytics_export_xlsx(request: Request, tab: str = "income", start: str | None = None,
                          end: str | None = None, session: Session = Depends(get_session)):
    from fastapi.responses import Response

    from app import analytics_export
    if tab not in TAB_LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab}")
    start_d, end_d = _resolve_range(start, end)
    if (end_d - start_d).days > MAX_SPAN_DAYS:
        raise HTTPException(status_code=400, detail="Date range too large")
    # `window` (not `data`) — the exporter's byte payload below is `data`.
    window = analytics.load_period(session, start_d, end_d)
    rows = _fetch_rows(tab, session, start_d, end_d, window)
    summary = analytics.period_summary(session, start_d, end_d, window)
    data = analytics_export.build_xlsx(TAB_LABELS[tab], start_d, end_d, summary, tab, rows)
    models.audit(session, request.state.user.id, "export.analytics", detail=f"{tab} xlsx")
    session.commit()
    filename = f"analytics-{tab}-{start_d}-{end_d}.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/analytics/export.pdf")
def analytics_export_pdf(request: Request, tab: str = "income", start: str | None = None,
                         end: str | None = None, session: Session = Depends(get_session)):
    from fastapi.responses import Response

    from app import analytics_export
    if tab not in TAB_LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab}")
    start_d, end_d = _resolve_range(start, end)
    if (end_d - start_d).days > MAX_SPAN_DAYS:
        raise HTTPException(status_code=400, detail="Date range too large")
    # `window` (not `data`) — the exporter's byte payload below is `data`.
    window = analytics.load_period(session, start_d, end_d)
    rows = _fetch_rows(tab, session, start_d, end_d, window)
    summary = analytics.period_summary(session, start_d, end_d, window)
    data = analytics_export.build_pdf(TAB_LABELS[tab], start_d, end_d, summary, tab, rows)
    models.audit(session, request.state.user.id, "export.analytics", detail=f"{tab} pdf")
    session.commit()
    filename = f"analytics-{tab}-{start_d}-{end_d}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# --- Rates tab ops: bulk raise preview/apply -------------------------------

def _rates_ctx(session: Session) -> dict:
    start_d, end_d = _current_month()
    return {
        "tab": "rates",
        "tab_labels": VIEW_LABELS,
        "start": start_d,
        "end": end_d,
        "quick": _quick_links(),
        "too_large": False,
        "rows": analytics.rate_review(session),
        "applied": None,
    }


def _parse_percent(raw: str | None) -> float | None:
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return None
    return pct if pct == pct else None  # NaN guard


def _validate_rate_form(percent: str | None, scope_tiers: bool,
                         scope_overrides: bool) -> tuple[float | None, str | None]:
    """Returns (percent-as-float-or-None, error-message-or-None)."""
    pct = _parse_percent(percent)
    if pct is None or pct <= 0 or pct > 100:
        return None, "Percent must be between 0 and 100"
    if not scope_tiers and not scope_overrides:
        return pct, "Pick at least one scope"
    return pct, None


def _tier_rows(session: Session) -> list["models.RateTier"]:
    return session.scalars(
        select(models.RateTier).order_by(models.RateTier.max_gallons.is_(None), models.RateTier.max_gallons)
    ).all()


def _override_props(session: Session) -> list["models.Property"]:
    return session.scalars(
        select(models.Property).join(models.Customer).where(
            models.Property.active.is_(True), models.Customer.active.is_(True),
            (models.Property.rate_override_cents.is_not(None)
             | models.Property.spa_rate_override_cents.is_not(None)))
    ).all()


def _preview_rows(session: Session, pct: float, scope_tiers: bool, scope_overrides: bool) -> list[dict]:
    rows = []
    if scope_tiers:
        for t in _tier_rows(session):
            rows.append({"label": t.label or "Tier", "old_cents": t.rate_cents,
                        "new_cents": round(t.rate_cents * (1 + pct / 100))})
    if scope_overrides:
        for p in _override_props(session):
            if p.rate_override_cents is not None:
                rows.append({"label": f"{p.customer.name} — {p.label}",
                            "old_cents": p.rate_override_cents,
                            "new_cents": round(p.rate_override_cents * (1 + pct / 100))})
            if p.spa_rate_override_cents is not None:
                rows.append({"label": f"{p.customer.name} — {p.label} (spa)",
                            "old_cents": p.spa_rate_override_cents,
                            "new_cents": round(p.spa_rate_override_cents * (1 + pct / 100))})
    return rows


@router.post("/analytics/rates/preview")
def rates_preview(request: Request, percent: str = Form(""),
                   scope_tiers: bool = Form(False), scope_overrides: bool = Form(False),
                   session: Session = Depends(get_session)):
    pct, error = _validate_rate_form(percent, scope_tiers, scope_overrides)
    ctx = _rates_ctx(session)
    ctx.update({
        "rate_error": error,
        "preview_percent": percent,
        "preview_scope_tiers": scope_tiers,
        "preview_scope_overrides": scope_overrides,
    })
    if error is None:
        ctx["preview_rows"] = _preview_rows(session, pct, scope_tiers, scope_overrides)
    return templates.TemplateResponse(request, "analytics.html", ctx)


@router.post("/analytics/rates/apply")
def rates_apply(request: Request, percent: str = Form(""),
                 scope_tiers: bool = Form(False), scope_overrides: bool = Form(False),
                 session: Session = Depends(get_session)):
    pct, error = _validate_rate_form(percent, scope_tiers, scope_overrides)
    if error is not None:
        ctx = _rates_ctx(session)
        ctx.update({
            "rate_error": error,
            "preview_percent": percent,
            "preview_scope_tiers": scope_tiers,
            "preview_scope_overrides": scope_overrides,
        })
        return templates.TemplateResponse(request, "analytics.html", ctx)

    affected = 0
    if scope_tiers:
        for t in _tier_rows(session):
            t.rate_cents = round(t.rate_cents * (1 + pct / 100))
            affected += 1
    if scope_overrides:
        for p in _override_props(session):
            if p.rate_override_cents is not None:
                p.rate_override_cents = round(p.rate_override_cents * (1 + pct / 100))
                affected += 1
            if p.spa_rate_override_cents is not None:
                p.spa_rate_override_cents = round(p.spa_rate_override_cents * (1 + pct / 100))
                affected += 1

    scope = "both" if scope_tiers and scope_overrides else ("tiers" if scope_tiers else "overrides")
    session.add(models.RateChange(percent=pct, scope=scope, affected_count=affected,
                                   user_id=request.state.user.id))
    models.audit(session, request.state.user.id, "rates.bulk_raise",
                 detail=f"{pct}% {scope} ({affected} affected)")
    session.commit()
    return RedirectResponse(f"/analytics?tab=rates&applied={affected}", status_code=303)

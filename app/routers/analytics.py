import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics, models
from app.db import get_session
from app.deps import require_admin
from app.main import templates
from app.routers.today import business_today

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


@router.get("/analytics")
def analytics_page(request: Request, tab: str = "income",
                    start: str | None = None, end: str | None = None,
                    applied: str | None = None,
                    session: Session = Depends(get_session)):
    if tab not in TAB_LABELS:
        tab = "income"

    start_d = _parse_date(start)
    end_d = _parse_date(end)
    if start_d is None or end_d is None:
        start_d, end_d = _current_month()
    elif start_d > end_d:
        start_d, end_d = end_d, start_d

    ctx = {
        "tab": tab,
        "tab_labels": TAB_LABELS,
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

    if tab == "income":
        rows = analytics.income_by_week(session, start_d, end_d)
        ctx["max_total"] = max((r.total_cents for r in rows), default=0)
    elif tab == "customers":
        rows = analytics.revenue_by_customer(session, start_d, end_d)
    elif tab == "crew":
        rows = analytics.revenue_by_tech(session, start_d, end_d)
    elif tab == "types":
        rows = analytics.revenue_by_job_type(session, start_d, end_d)
    elif tab == "chemicals":
        rows = analytics.chem_margin(session, start_d, end_d)
    elif tab == "volume":
        rows = analytics.visit_volume(session, start_d, end_d)
    else:  # rates
        rows = analytics.rate_review(session)

    ctx["rows"] = rows
    return templates.TemplateResponse(request, "analytics.html", ctx)


def _format_cents(cents: int | None) -> str:
    """Format cents as dollars with 2 decimal places, or empty string if None."""
    if cents is None:
        return ""
    return f"{cents / 100:.2f}"


@router.get("/analytics/export")
def analytics_export(tab: str = "income",
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

    # Fetch data for the requested tab
    if tab == "income":
        rows = analytics.income_by_week(session, start_d, end_d)
    elif tab == "customers":
        rows = analytics.revenue_by_customer(session, start_d, end_d)
    elif tab == "crew":
        rows = analytics.revenue_by_tech(session, start_d, end_d)
    elif tab == "types":
        rows = analytics.revenue_by_job_type(session, start_d, end_d)
    elif tab == "chemicals":
        rows = analytics.chem_margin(session, start_d, end_d)
    elif tab == "volume":
        rows = analytics.visit_volume(session, start_d, end_d)
    else:  # rates
        rows = analytics.rate_review(session)

    # Generate CSV
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    if tab == "income":
        writer.writerow(["week_start", "iso_week", "labor", "chemicals_cost", "chemicals_billable", "jobs", "total"])
        for r in rows:
            writer.writerow([
                r.week_start_date,
                r.iso_week,
                _format_cents(r.labor_cents),
                _format_cents(r.chem_cost_cents),
                _format_cents(r.chem_billable_cents),
                _format_cents(r.job_cents),
                _format_cents(r.total_cents),
            ])
    elif tab == "customers":
        writer.writerow(["customer", "visits", "labor", "chemicals", "jobs", "total"])
        for r in rows:
            writer.writerow([
                r.customer.name,
                r.visit_count,
                _format_cents(r.labor_cents),
                _format_cents(r.chem_billable_cents),
                _format_cents(r.job_cents),
                _format_cents(r.total_cents),
            ])
    elif tab == "crew":
        writer.writerow(["tech", "visits", "jobs", "minutes", "revenue", "revenue_per_hour"])
        for r in rows:
            tech_name = r.user.name if r.user else "Unattributed"
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
                r.label,
                _format_cents(r.total_cents),
            ])
    elif tab == "chemicals":
        writer.writerow(["chemical", "quantity", "unit", "cost", "billable", "margin"])
        for r in rows:
            writer.writerow([
                r.chemical.name,
                f"{r.quantity:.2f}",
                r.chemical.unit,
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
                r.property.customer.name,
                r.property.label,
                _format_cents(r.current_cents),
                _format_cents(r.tier_cents),
                _format_cents(r.delta_cents),
            ])

    csv_content = output.getvalue()
    filename = f"analytics-{tab}-{start_d.isoformat()}-{end_d.isoformat()}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# --- Rates tab ops: bulk raise preview/apply -------------------------------

def _rates_ctx(session: Session) -> dict:
    start_d, end_d = _current_month()
    return {
        "tab": "rates",
        "tab_labels": TAB_LABELS,
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
            models.Property.rate_override_cents.is_not(None))
    ).all()


def _preview_rows(session: Session, pct: float, scope_tiers: bool, scope_overrides: bool) -> list[dict]:
    rows = []
    if scope_tiers:
        for t in _tier_rows(session):
            rows.append({"label": t.label or "Tier", "old_cents": t.rate_cents,
                        "new_cents": round(t.rate_cents * (1 + pct / 100))})
    if scope_overrides:
        for p in _override_props(session):
            rows.append({"label": f"{p.customer.name} — {p.label}",
                        "old_cents": p.rate_override_cents,
                        "new_cents": round(p.rate_override_cents * (1 + pct / 100))})
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
            p.rate_override_cents = round(p.rate_override_cents * (1 + pct / 100))
            affected += 1

    scope = "both" if scope_tiers and scope_overrides else ("tiers" if scope_tiers else "overrides")
    session.add(models.RateChange(percent=pct, scope=scope, affected_count=affected,
                                   user_id=request.state.user.id))
    session.commit()
    return RedirectResponse(f"/analytics?tab=rates&applied={affected}", status_code=303)

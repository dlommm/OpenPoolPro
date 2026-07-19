from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import analytics
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

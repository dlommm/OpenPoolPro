import logging
from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inventory, models, qbo, receivables, water_alerts
from app.db import get_session
from app.deps import require_admin
from app.maintenance import warranty_alerts
from app.scheduling import (ScheduleWindow, missed_stops, progress_segments, stops_for_date,
                            visible_to)
from app.templating import templates
from app.timefmt import business_today, local_now

log = logging.getLogger("openpoolpro.app")
router = APIRouter()


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_session),
              user=Depends(require_admin)):
    """The owner's morning screen (design v0.6): the day's route, what needs a
    decision, money owed, and the few things running out."""
    d = business_today()
    rows = qbo.month_rollup(session, d.year, d.month)
    # Shared window for today plus the 14-day lookback — the dashboard was the
    # heaviest page in the app, paying the full scheduler pass 15 times over.
    window = ScheduleWindow(session, d - timedelta(days=14), d)
    stops = stops_for_date(session, d, window=window)
    missed = missed_stops(session, d, window=window)
    open_jobs = session.scalars(select(models.Job).where(
        models.Job.status == "open")).all()
    people = {u.id: u for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    counts = Counter(s_.effective_tech_id for s_ in stops if s_.effective_tech_id is not None)
    # Everyone working a route today, the signed-in person first: the owner
    # holds a route like anyone else (design v0.5).
    lanes = []
    for tid in sorted(counts, key=lambda t: (t != user.id, people[t].name if t in people else "")):
        if tid not in people:
            continue
        mine = [s_ for s_ in stops if s_.effective_tech_id == tid]
        lanes.append({"person": people[tid], "count": len(mine), "segs": progress_segments(mine),
                      "done": sum(1 for s_ in mine if s_.status != "due")})
    unassigned = [s_ for s_ in stops if s_.effective_tech_id is None]
    low_chems = inventory.low_stock(session)
    warranty_items = warranty_alerts(session, d)
    ar_rows = receivables.by_customer(session, d)
    overdue = [inv for r in ar_rows for inv in r.invoices if inv.days_overdue(d) > 0]
    try:
        alerts = water_alerts.current(session, d)
    except Exception:
        log.warning("water alerts failed; dashboard renders without them", exc_info=True)
        alerts = []
    review = session.scalars(select(models.QBODraft).where(
        models.QBODraft.status.in_(("draft", "mismatch", "delivery_unknown", "create_unknown")))).all()
    unsigned = session.scalars(select(models.Contract).where(models.Contract.status == "sent")).all()

    needs = []
    if review:
        odd = [r for r in review if r.status != "draft"]
        needs.append({"title": f"{len(review)} QuickBooks draft{'s' if len(review) != 1 else ''} to review",
                      "sub": (f"{len(odd)} need attention: totals differ or delivery unconfirmed"
                              if odd else "Prepared invoices waiting for approval"),
                      "href": "/billing", "cta": "Review"})
    if missed:
        needs.append({"title": f"{len(missed)} missed stop{'s' if len(missed) != 1 else ''}",
                      "sub": ", ".join(m.property.customer.name for m in missed[:3])
                      + ("…" if len(missed) > 3 else ""),
                      "href": "/?crew=1", "cta": "Decide"})
    if unassigned:
        needs.append({"title": f"{len(unassigned)} stop{'s' if len(unassigned) != 1 else ''} today "
                               f"with no technician",
                      "sub": ", ".join(s_.property.customer.name for s_ in unassigned[:3]),
                      "href": "/routes/balance", "cta": "Assign"})
    if overdue:
        needs.append({"title": f"{len(overdue)} invoice{'s' if len(overdue) != 1 else ''} overdue",
                      "sub": f"${sum(i.balance_cents for i in overdue) / 100:,.2f} past due",
                      "href": "/ar", "cta": "Follow up"})
    if alerts:
        needs.append({"title": f"{len(alerts)} water alert{'s' if len(alerts) != 1 else ''}",
                      "sub": ", ".join(sorted({a['property'].customer.name for a in alerts})[:3]),
                      "href": "/water-alerts", "cta": "Review"})
    if unsigned:
        needs.append({"title": f"{len(unsigned)} contract{'s' if len(unsigned) != 1 else ''} "
                               f"waiting for a signature",
                      "sub": "Sent, not signed yet", "href": "/contracts", "cta": "Open"})

    # Six months of revenue, oldest first; the current month is partial.
    months = []
    y, m = d.year, d.month
    for _ in range(6):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    revenue = []
    for (yy, mm) in reversed(months):
        total = (sum(r.total_cents for r in rows) if (yy, mm) == (d.year, d.month)
                 else sum(r.total_cents for r in qbo.month_rollup(session, yy, mm)))
        revenue.append({"label": date(yy, mm, 1).strftime("%b"), "cents": total,
                        "current": (yy, mm) == (d.year, d.month)})
    peak = max([r["cents"] for r in revenue] + [1])
    scale = nice_ceiling(peak)
    for r in revenue:
        r["pct"] = round(r["cents"] / scale * 100, 1)
    hour = local_now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    return templates.TemplateResponse(request, "dashboard.html", {
        "revenue_mtd_cents": sum(r.total_cents for r in rows),
        "stops_done": sum(1 for s_ in stops if s_.status != "due"),
        "stops_total": len(stops), "missed_count": len(missed),
        "open_jobs": len(open_jobs),
        "high_jobs": sum(1 for j in open_jobs if j.priority == "high"),
        "lanes": lanes, "unassigned": unassigned, "needs": needs,
        "ar_total_cents": receivables.totals(ar_rows)["total_cents"],
        "overdue_cents": sum(i.balance_cents for i in overdue),
        "alert_count": len(alerts), "revenue": revenue, "revenue_scale": scale,
        "greeting": greeting, "first_name": (user.name or "").split(" ")[0],
        "warranty_items": warranty_items, "d": d, "low_chems": low_chems})


def nice_ceiling(cents: int) -> int:
    """Round a chart maximum up to a readable gridline value (1, 2, 4, 5, 8 ×
    a power of ten), so the axis says $12k rather than $11,387."""
    import math
    if cents <= 0:
        return 100
    exp = 10 ** int(math.floor(math.log10(cents)))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if cents <= step * exp:
            return int(step * exp)
    return int(10 * exp)


@router.get("/today/print")
def route_sheet(request: Request, tech: int | None = None,
                session: Session = Depends(get_session)):
    user = request.state.user
    d = business_today()
    stops = stops_for_date(session, d)
    jobs = session.scalars(select(models.Job).where(
        models.Job.status == "scheduled", models.Job.scheduled_date == d)).all()
    active_ids = {u.id for u in session.scalars(
        select(models.User).where(models.User.active)).all()}
    if not user.can("view_all_routes"):
        stops = [s_ for s_ in stops if visible_to(user, s_.property.assigned_tech_id, active_ids)]
        jobs = [j for j in jobs if visible_to(user, j.assigned_tech_id, active_ids)]
    elif tech is not None:
        stops = [s_ for s_ in stops if s_.property.assigned_tech_id == tech]
        jobs = [j for j in jobs if j.assigned_tech_id == tech]
    who = user if not user.can("view_all_routes") else (
        session.get(models.User, tech) if tech is not None else None)
    return templates.TemplateResponse(request, "print_route.html",
                                      {"stops": stops, "jobs": jobs, "d": d,
                                       "who": who.name if who is not None else ""})


@router.get("/search")
def search(request: Request, q: str = "", session: Session = Depends(get_session)):
    q = (q or "").strip()[:80]
    customers = properties = []
    if q:
        like = f"%{q}%"
        customers = session.scalars(select(models.Customer).where(
            models.Customer.active,
            models.Customer.name.ilike(like)
            | models.Customer.phone.ilike(like)
            | models.Customer.email.ilike(like)).order_by(models.Customer.name)
            .limit(25)).all()
        properties = session.scalars(select(models.Property).join(models.Customer).where(
            models.Property.active, models.Customer.active,
            models.Property.address.ilike(like) | models.Property.label.ilike(like))
            .order_by(models.Customer.name).limit(25)).all()
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "customers": customers, "properties": properties})

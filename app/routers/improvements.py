from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import followups, models, operations, route_planning, water_alerts
from app.db import get_session
from app.deps import require_admin, require_perm
from app.templating import templates
from app.timefmt import business_today

router = APIRouter()


@router.get("/operations", dependencies=[Depends(require_admin)])
def operations_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "improvements/operations.html", {
        **operations.overview(session, request.app)})


@router.get("/routes/plan", dependencies=[Depends(require_perm("view_all_routes"))])
def plan_page(request: Request, weekday: int = Query(0, ge=0, le=6), tech_id: int = Query(0, ge=0),
              optimize: bool = False, session: Session = Depends(get_session)):
    rows = route_planning.properties(session, weekday, tech_id)
    suggested = route_planning.suggest(rows) if optimize else rows
    techs = session.scalars(select(models.User).where(models.User.active).order_by(models.User.name)).all()
    return templates.TemplateResponse(request, "improvements/plan.html", {
        "rows": suggested, "weekday": weekday, "tech_id": tech_id, "techs": techs,
        "before": route_planning.estimate(rows), "after": route_planning.estimate(suggested),
        "fingerprint": route_planning.fingerprint(rows),
        "coordinates": {p.id: route_planning.coordinates(p) for p in rows},
        # Stop counts for the weekday and technician chips (design v0.13).
        "day_counts": [len(route_planning.properties(session, d, tech_id)) for d in range(7)],
        "tech_counts": {t.id: len(route_planning.properties(session, weekday, t.id))
                        for t in [*techs, None] if t is not None} | {
            0: len(route_planning.properties(session, weekday, 0))}})


@router.post("/routes/plan", dependencies=[Depends(require_perm("view_all_routes"))])
async def apply_plan(request: Request, weekday: int = Form(..., ge=0, le=6),
                     tech_id: int = Form(0, ge=0), fingerprint: str = Form(...),
                     session: Session = Depends(get_session)):
    rows = route_planning.properties(session, weekday, tech_id)
    if fingerprint != route_planning.fingerprint(rows):
        raise HTTPException(409, "The route changed. Reload the planner before saving.")
    form = await request.form()
    try:
        ids = [int(v) for v in form.getlist("property_id")]
    except ValueError:
        raise HTTPException(400, "Invalid stop order") from None
    if len(ids) != len(rows) or set(ids) != {p.id for p in rows}:
        raise HTTPException(400, "The stop list changed. Reload the planner.")
    order = {pid: i for i, pid in enumerate(ids)}
    for prop in rows:
        prop.route_order = order[prop.id]
    models.audit(session, request.state.user.id, "route.order", f"weekday={weekday} tech={tech_id} stops={len(ids)}")
    session.commit()
    return RedirectResponse(f"/routes/plan?weekday={weekday}&tech_id={tech_id}&saved=1", 303)


@router.get("/followups", dependencies=[Depends(require_admin)])
def followups_page(request: Request, session: Session = Depends(get_session)):
    history = session.scalars(select(models.InvoiceFollowup).order_by(models.InvoiceFollowup.id.desc())
                              .limit(50)).all()
    return templates.TemplateResponse(request, "improvements/followups.html", {
        "rows": followups.candidates(session, business_today()), "history": history,
        "days": ",".join(map(str, followups.stages(session)))})


@router.post("/followups/settings", dependencies=[Depends(require_admin)])
def followup_settings(request: Request, days: str = Form(..., max_length=50),
                      session: Session = Depends(get_session)):
    try:
        values = sorted({int(v.strip()) for v in days.split(",")})
    except ValueError:
        raise HTTPException(400, "Use comma-separated overdue days, such as 7,14,30.") from None
    if not 1 <= len(values) <= 6 or not all(1 <= v <= 365 for v in values):
        raise HTTPException(400, "Choose one to six thresholds between 1 and 365 days.")
    models.set_setting(session, "followup_days", ",".join(map(str, values)))
    models.audit(session, request.state.user.id, "followup.settings")
    session.commit()
    return RedirectResponse("/followups", 303)


@router.post("/followups/approve", dependencies=[Depends(require_admin)])
async def approve_followups(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    selected = set(form.getlist("snapshot"))
    if not selected or len(selected) > 100:
        raise HTTPException(400, "Select between 1 and 100 reminders to approve.")
    rows = {r["snapshot"]: r for r in followups.candidates(session, business_today())}
    if not selected <= rows.keys():
        raise HTTPException(409, "An invoice or recipient changed. Review the refreshed previews before approving.")
    try:
        for fingerprint in selected:
            followups.approve(session, rows[fingerprint], request.state.user.id)
        models.audit(session, request.state.user.id, "followup.approved", f"count={len(selected)}")
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A reminder was already approved. Refresh to see its delivery status.") from None
    return RedirectResponse("/followups?approved=1", 303)


@router.get("/water-alerts", dependencies=[Depends(require_admin)])
def water_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "improvements/water.html", {
        "alerts": water_alerts.current(session, business_today())})


@router.post("/water-alerts/inspect", dependencies=[Depends(require_admin)])
def inspect_water(request: Request, fingerprint: str = Form(..., max_length=64),
                  session: Session = Depends(get_session)):
    alerts = {a["fingerprint"]: a for a in water_alerts.current(session, business_today())}
    alert = alerts.get(fingerprint)
    if alert is None:
        raise HTTPException(409, "Readings changed. Refresh the alerts before creating a job.")
    if alert["job_id"]:
        return RedirectResponse(f"/jobs/{alert['job_id']}", 303)
    prop = alert["property"]
    job = models.Job(property_id=prop.id, title=f"Inspect water: {alert['label']}",
        description=f"{alert['reason']}. Recent readings: {alert['values']}. "
                    f"Configured target band: {alert['low']:g}–{alert['high']:g}. Retest and inspect before treatment.",
        assigned_tech_id=prop.assigned_tech_id, status="open", priority="normal", labor_cents=0)
    try:
        session.add(job)
        session.flush()
        session.add(models.JobTask(job_id=job.id,
            label="Retest water and inspect equipment before treatment", position=0))
        session.add(models.WaterReview(fingerprint=fingerprint, property_id=prop.id, job_id=job.id))
        models.audit(session, request.state.user.id, "water.inspection", f"property={prop.id} job={job.id}")
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "An inspection was already created. Refresh the alerts.") from None
    return RedirectResponse(f"/jobs/{job.id}", 303)

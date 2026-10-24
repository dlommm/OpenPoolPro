from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.db import get_or_404, get_session
from app.deps import require_perm
from app.route_planning import ROUTE_ROLES, route_people
from app.scheduling import balance_moves, suggest_tech, weekday_load
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_perm("view_all_routes"))])

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_int(raw) -> int | None:
    """Tolerant parse: blank/garbage/non-int -> None. Never 500s on a bad
    form value or query param."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _active_techs(session: Session) -> list[models.User]:
    return session.scalars(select(models.User).where(
        models.User.active, models.User.role == "tech").order_by(models.User.id)).all()


def _suggested_tech(session: Session, weekday: int, tech_ids: list[int],
                     counts: dict[int, int]) -> int | None:
    """suggest_tech's majority pick, falling back to the lowest-load active
    tech when that pick isn't one of our active-tech balance targets (e.g.
    the majority is an admin/owner or a disabled tech)."""
    tid = suggest_tech(session, weekday)
    if tid in tech_ids:
        return tid
    if not tech_ids:
        return None
    return min(tech_ids, key=lambda t: (counts.get(t, 0), t))


def _build_context(session: Session, preview: bool) -> dict:
    techs = _active_techs(session)
    tech_ids = [t.id for t in techs]
    load = weekday_load(session)
    people = route_people(session)
    # Owners appear as a read-only "by hand" column, only when they hold a
    # recurring stop: the balancer neither counts nor moves those stops.
    owners = [u for u in people if u.role == "admin"
              and any(load.get(wd, {}).get(u.id) for wd in range(7))]
    tech_by_id = {u.id: u for u in people}

    matrix: dict[int, dict[int | None, int]] = {}
    unassigned_by_weekday: dict[int, list[models.Property]] = {}
    suggested_by_weekday: dict[int, int | None] = {}
    moves: list[tuple[int, models.Property, int | None, int]] = []

    for wd in range(7):
        load_wd = load.get(wd, {})
        counts = {tid: len(load_wd.get(tid, [])) for tid in tech_ids}
        matrix[wd] = {**counts, **{o.id: len(load_wd.get(o.id, [])) for o in owners},
                      None: len(load_wd.get(None, []))}
        unassigned = sorted(load_wd.get(None, []), key=lambda p: (p.route_order, p.id))
        if unassigned:
            unassigned_by_weekday[wd] = unassigned
            suggested_by_weekday[wd] = _suggested_tech(session, wd, tech_ids, counts)
        if preview:
            for prop, dest in balance_moves(load_wd, tech_ids):
                moves.append((wd, prop, prop.assigned_tech_id, dest))

    # Busiest technician cell, for the heat shading of the grid.
    peak = max([1, *(matrix[wd].get(t, 0) for wd in range(7) for t in tech_ids)])
    return {
        "techs": techs, "owners": owners, "people": people, "tech_by_id": tech_by_id,
        "weekday_names": WEEKDAY_NAMES, "matrix": matrix, "peak": peak,
        "unassigned_by_weekday": unassigned_by_weekday,
        "suggested_by_weekday": suggested_by_weekday, "preview": preview, "moves": moves,
    }


@router.get("/routes/balance")
def balance_page(request: Request, preview: str | None = None, applied: str | None = None,
                  session: Session = Depends(get_session)):
    applied_n = _parse_int(applied)
    banner = f"{applied_n} properties reassigned" if applied_n is not None else None
    ctx = _build_context(session, preview=bool(preview))
    return templates.TemplateResponse(request, "routes/balance.html", {**ctx, "banner": banner})


def _assign(request: Request, session: Session, property_id: int,
            tech_id: int) -> models.Property:
    """Hand one property to a person's route: an active technician, or an
    active owner chosen explicitly (owner routes are only ever set by hand,
    never by the balancer). The stop joins the end of that person's route for
    its weekday. Unknown ids 404; an inactive property or person, or a role
    that can't hold a route, is a 400."""
    prop = get_or_404(session, models.Property, property_id)
    person = get_or_404(session, models.User, tech_id)
    if not prop.active:
        raise HTTPException(400, "That property is inactive.")
    if not person.active or person.role not in ROUTE_ROLES:
        raise HTTPException(400, "Choose an active technician or owner.")
    if prop.assigned_tech_id != person.id:
        last = session.scalar(select(func.max(models.Property.route_order)).where(
            models.Property.assigned_tech_id == person.id,
            models.Property.weekday == prop.weekday, models.Property.id != prop.id))
        prop.assigned_tech_id = person.id
        prop.route_order = (last if last is not None else -1) + 1
        models.audit(session, request.state.user.id, "route.assign",
                     detail=f"property={prop.id} tech={person.id}"
                            + (" (owner, by hand)" if person.role == "admin" else ""))
        session.commit()
    return prop


@router.post("/routes/assign")
def assign(request: Request, property_id: int = Form(...), tech_id: int = Form(...),
           session: Session = Depends(get_session)):
    _assign(request, session, property_id, tech_id)
    return RedirectResponse("/routes/balance", status_code=303)


@router.post("/routes/plan/assign")
def assign_from_plan(request: Request, property_id: int = Form(...), tech_id: int = Form(...),
                     from_tech_id: str = Form("", max_length=12),
                     session: Session = Depends(get_session)):
    """The planner's "Move a stop" form: same rules as /routes/assign, then
    back to the route the stop came from. The URL is built here from ints,
    never taken from the form, so it can't become an open redirect."""
    prop = _assign(request, session, property_id, tech_id)
    src = _parse_int(from_tech_id)
    src = src if src is not None and src >= 0 else 0
    return RedirectResponse(
        f"/routes/plan?weekday={prop.weekday % 7}&tech_id={src}&assigned={tech_id}",
        status_code=303)


@router.post("/routes/apply")
def apply_balance(request: Request, session: Session = Depends(get_session)):
    # Technicians only: owner-held stops sit outside the headcount and an
    # owner is never a destination (balance_moves only moves between tech_ids).
    tech_ids = [t.id for t in _active_techs(session)]
    load = weekday_load(session)
    count = 0
    for wd in range(7):
        for prop, dest in balance_moves(load.get(wd, {}), tech_ids):
            prop.assigned_tech_id = dest
            count += 1
    if count:
        models.audit(session, request.state.user.id, "route.balance_applied",
                     detail=f"{count} properties reassigned")
    session.commit()
    return RedirectResponse(f"/routes/balance?applied={count}", status_code=303)

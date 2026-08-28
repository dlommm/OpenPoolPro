from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.db import get_or_404, get_session
from app.deps import require_perm
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
    tech_by_id = {t.id: t for t in techs}
    load = weekday_load(session)

    matrix: dict[int, dict[int | None, int]] = {}
    unassigned_by_weekday: dict[int, list[models.Property]] = {}
    suggested_by_weekday: dict[int, int | None] = {}
    moves: list[tuple[int, models.Property, int | None, int]] = []

    for wd in range(7):
        load_wd = load.get(wd, {})
        counts = {tid: len(load_wd.get(tid, [])) for tid in tech_ids}
        matrix[wd] = {**counts, None: len(load_wd.get(None, []))}
        unassigned = sorted(load_wd.get(None, []), key=lambda p: (p.route_order, p.id))
        if unassigned:
            unassigned_by_weekday[wd] = unassigned
            suggested_by_weekday[wd] = _suggested_tech(session, wd, tech_ids, counts)
        if preview:
            for prop, dest in balance_moves(load_wd, tech_ids):
                moves.append((wd, prop, prop.assigned_tech_id, dest))

    return {
        "techs": techs, "tech_by_id": tech_by_id, "weekday_names": WEEKDAY_NAMES,
        "matrix": matrix, "unassigned_by_weekday": unassigned_by_weekday,
        "suggested_by_weekday": suggested_by_weekday, "preview": preview, "moves": moves,
    }


@router.get("/routes/balance")
def balance_page(request: Request, preview: str | None = None, applied: str | None = None,
                  session: Session = Depends(get_session)):
    applied_n = _parse_int(applied)
    banner = f"{applied_n} properties reassigned" if applied_n is not None else None
    ctx = _build_context(session, preview=bool(preview))
    return templates.TemplateResponse(request, "routes/balance.html", {**ctx, "banner": banner})


@router.post("/routes/assign")
def assign(request: Request, property_id: int = Form(...), tech_id: int = Form(...),
           session: Session = Depends(get_session)):
    prop = get_or_404(session, models.Property, property_id)
    tech = get_or_404(session, models.User, tech_id)
    if prop.active and tech.active and tech.role == "tech":
        prop.assigned_tech_id = tech.id
        models.audit(session, request.state.user.id, "route.assign",
                     detail=f"property={prop.id} tech={tech.id}")
        session.commit()
    return RedirectResponse("/routes/balance", status_code=303)


@router.post("/routes/apply")
def apply_balance(request: Request, session: Session = Depends(get_session)):
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

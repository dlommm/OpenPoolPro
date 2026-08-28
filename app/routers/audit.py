import math

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.db import get_session
from app.deps import require_admin
from app.forms import parse_page
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

PAGE_SIZE = 100


def _parse_user_id(raw: str | None) -> int | None:
    """Tolerant user_id parse: blank/garbage means "no filter"."""
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _escape_like(raw: str) -> str:
    """Escape LIKE metacharacters in user-supplied text so a prefix filter
    containing a literal % or _ can't turn into an unintended wildcard."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/audit")
def audit_page(request: Request, action: str = "", user_id: str | None = None,
               page: str | None = None, session: Session = Depends(get_session)):
    action = (action or "").strip()
    uid = _parse_user_id(user_id)
    p = parse_page(page)

    filters = []
    if action:
        filters.append(models.AuditLog.action.like(f"{_escape_like(action)}%", escape="\\"))
    if uid is not None:
        filters.append(models.AuditLog.user_id == uid)

    count_q = select(func.count()).select_from(models.AuditLog)
    rows_q = select(models.AuditLog)
    for f in filters:
        count_q = count_q.where(f)
        rows_q = rows_q.where(f)

    total = session.scalar(count_q) or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    rows = session.scalars(
        rows_q.order_by(models.AuditLog.at.desc(), models.AuditLog.id.desc())
              .offset((p - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()

    users = session.scalars(select(models.User).order_by(
        models.User.active.desc(), models.User.name)).all()
    users_by_id = {u.id: (u.username or u.name) for u in users}

    return templates.TemplateResponse(request, "audit.html", {
        "rows": rows, "users": users, "users_by_id": users_by_id,
        "action": action, "user_id": uid, "page": p, "total_pages": total_pages,
        "total": total, "has_next": total > p * PAGE_SIZE,
    })

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app import models
from app.db import get_session
from app.deps import require_perm
from app.main import templates
from app.qbo import export_csv, month_rollup
from app.timefmt import parse_ym

router = APIRouter(dependencies=[Depends(require_perm("view_money"))])


@router.get("/money")
def money_page(request: Request, ym: str | None = None,
               session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    rows = month_rollup(session, year, month)
    prev = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    nxt = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"
    grand = sum(r.total_cents for r in rows)
    grand_tax = sum(r.tax_cents for r in rows)
    return templates.TemplateResponse(request, "money.html", {
        "rows": rows, "year": year, "month": month,
        "ym": f"{year}-{month:02d}", "prev": prev, "next": nxt,
        "grand": grand, "grand_tax": grand_tax,
        "grand_total": grand + grand_tax})


@router.get("/money/export")
def money_export(request: Request, ym: str | None = None, session: Session = Depends(get_session)):
    year, month = parse_ym(ym)
    csv_text = export_csv(session, year, month)
    models.audit(session, request.state.user.id, "export.qbo", detail=f"{year}-{month:02d}")
    session.commit()
    return PlainTextResponse(csv_text, media_type="text/csv", headers={
        "Content-Disposition": f"attachment; filename=qbo-{year}-{month:02d}.csv"})

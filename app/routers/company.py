import os

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import models
from app.company import COMPANY_KEYS, MAX_LOGO_BYTES, company_info, logo_path, store_logo  # noqa: F401
from app.db import get_session
from app.deps import require_admin
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])
serve_router = APIRouter()



@router.post("/settings/company")
async def save_company(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for key in COMPANY_KEYS:
        models.set_setting(session, key, (form.get(key) or "").strip())
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/company/logo")
async def upload_logo(request: Request, file: UploadFile,
                      session: Session = Depends(get_session)):
    problem = store_logo(await file.read(), file.content_type)
    if problem:
        return templates.TemplateResponse(request, "settings/_logo_error.html",
                                          {"message": problem})
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/company/logo/remove")
def remove_logo():
    path = logo_path()
    if os.path.isfile(path):
        os.remove(path)
    return RedirectResponse("/settings", status_code=303)


@serve_router.get("/branding/logo")
def serve_logo():
    path = logo_path()
    if not os.path.isfile(path):
        from fastapi.responses import Response
        return Response(status_code=404)
    return FileResponse(path, media_type="image/png")

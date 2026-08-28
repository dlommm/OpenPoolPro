import io
import os

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image
from sqlalchemy.orm import Session

from app import models
from app.company import COMPANY_KEYS, company_info, logo_path  # noqa: F401
from app.db import get_session
from app.deps import require_admin
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])
serve_router = APIRouter()

MAX_LOGO_BYTES = 2 * 1024 * 1024


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
    if file.content_type not in ("image/png", "image/jpeg"):
        return templates.TemplateResponse(request, "settings/_logo_error.html",
                                          {"message": "Logo must be a PNG or JPEG"})
    data = await file.read()
    if len(data) > MAX_LOGO_BYTES:
        return templates.TemplateResponse(request, "settings/_logo_error.html",
                                          {"message": "Logo must be under 2 MB"})
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG"):
            return templates.TemplateResponse(request, "settings/_logo_error.html",
                                              {"message": "Logo must be a PNG or JPEG"})
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    except Exception:
        return templates.TemplateResponse(request, "settings/_logo_error.html",
                                          {"message": "That file isn't a readable logo image (PNG/JPEG)"})
    img.thumbnail((600, 600))
    img.save(logo_path(), "PNG")
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

import io
import os

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image
from sqlalchemy.orm import Session

from app import config, models
from app.db import get_session
from app.deps import require_admin
from app.main import templates

router = APIRouter(dependencies=[Depends(require_admin)])
serve_router = APIRouter()

MAX_LOGO_BYTES = 2 * 1024 * 1024
COMPANY_KEYS = ("business_name", "company_address", "company_phone",
                "company_email", "company_license")


def _logo_path() -> str:
    d = os.path.join(config.DATA_DIR, "branding")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "logo.png")


def company_info(session) -> dict:
    info = {k: models.get_setting(session, k, "") for k in COMPANY_KEYS}
    info["has_logo"] = os.path.isfile(_logo_path())
    return info


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
    img.save(_logo_path(), "PNG")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/company/logo/remove")
def remove_logo():
    path = _logo_path()
    if os.path.isfile(path):
        os.remove(path)
    return RedirectResponse("/settings", status_code=303)


@serve_router.get("/branding/logo")
def serve_logo():
    path = _logo_path()
    if not os.path.isfile(path):
        from fastapi.responses import Response
        return Response(status_code=404)
    return FileResponse(path, media_type="image/png")

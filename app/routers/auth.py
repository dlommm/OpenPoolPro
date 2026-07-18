import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import config
from app.main import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if not secrets.compare_digest(password, config.APP_PASSWORD):
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password"})
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

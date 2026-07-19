from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.auth_core import hash_password, verify_password
from app.db import get_session
from app.main import templates

# Verified against when the username is unknown, so both failure paths pay
# the same bcrypt cost (no timing-based username enumeration).
_DUMMY_HASH = hash_password("timing-equalizer")

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""),
          session: Session = Depends(get_session)):
    user = session.scalars(select(models.User).where(
        models.User.username == username.strip().lower(),
        models.User.active)).first()
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not ok:
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Wrong username or password"})
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

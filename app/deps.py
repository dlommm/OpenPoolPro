from fastapi import Depends, HTTPException, Request

from app import models


def current_user(request: Request) -> models.User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def require_admin(user: models.User = Depends(current_user)) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=403)
    return user


def require_perm(flag: str):
    def dep(user: models.User = Depends(current_user)) -> models.User:
        if not user.can(flag):
            raise HTTPException(status_code=403)
        return user
    return dep

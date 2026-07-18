from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import models, photos_svc
from app.db import get_session

router = APIRouter()

ALLOWED = {"pool", "visit", "job"}


@router.get("/photos/file/{photo_id}")
def serve(photo_id: int, thumb: int = 0, session: Session = Depends(get_session)):
    ph = session.get(models.Photo, photo_id)
    if ph is None:
        return HTMLResponse("gone", status_code=404)
    return FileResponse(photos_svc.path_for(ph, thumb=bool(thumb)), media_type="image/jpeg")


@router.post("/photos/{photo_id}/delete", response_class=HTMLResponse)
def delete(photo_id: int, session: Session = Depends(get_session)):
    photos_svc.delete_photo(session, photo_id)
    session.flush()
    session.commit()
    return HTMLResponse("")


@router.post("/photos/{entity_type}/{entity_id}", response_class=HTMLResponse)
async def upload(entity_type: str, entity_id: int, files: list[UploadFile],
                 session: Session = Depends(get_session)):
    if entity_type not in ALLOWED:
        return HTMLResponse("bad entity", status_code=400)
    frags = []
    for f in files:
        data = await f.read()
        try:
            ph = photos_svc.save_photo(session, entity_type, entity_id, data)
            session.commit()
            frags.append(
                f"<img src='/photos/file/{ph.id}?thumb=1' class='w-20 h-20 object-cover rounded-lg'>")
        except ValueError:
            session.rollback()
            frags.append(f"<span class='text-red-600 text-sm'>{f.filename} failed — retry</span>")
    return HTMLResponse("".join(frags))

import html

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import models, photos_svc
from app.db import get_session
from app.deps import require_perm

router = APIRouter()

ALLOWED = {"pool", "visit", "job", "property"}
# Cap raw upload bytes before decoding so a huge (or decompression-bomb) file
# can't pin server memory. Field photos are re-encoded down to a 2000px JPEG,
# so 20 MB of source is generous headroom.
MAX_PHOTO_BYTES = 20 * 1024 * 1024


@router.get("/photos/file/{photo_id}")
def serve(photo_id: int, thumb: int = 0, session: Session = Depends(get_session)):
    ph = session.get(models.Photo, photo_id)
    if ph is None:
        return HTMLResponse("gone", status_code=404)
    return FileResponse(photos_svc.path_for(ph, thumb=bool(thumb)), media_type="image/jpeg")


@router.post("/photos/{photo_id}/delete", response_class=HTMLResponse)
def delete(photo_id: int, session: Session = Depends(get_session),
           user=Depends(require_perm("manage_customers"))):
    """Deleting is permanent — the row goes and both files are unlinked from
    disk. Uploading stays open to any signed-in tech (capturing before/after
    evidence is the job), but destroying that evidence is not."""
    photos_svc.delete_photo(session, photo_id)
    session.flush()
    session.commit()
    return HTMLResponse("")


@router.post("/photos/{entity_type}/{entity_id}", response_class=HTMLResponse)
async def upload(entity_type: str, entity_id: int, files: list[UploadFile],
                 tag: str = Form(""), session: Session = Depends(get_session)):
    if entity_type not in ALLOWED:
        return HTMLResponse("bad entity", status_code=400)
    tag = tag if tag in models.PHOTO_TAGS else ""  # tolerant: unknown/blank -> no tag
    frags = []
    for f in files:
        # Check the size Starlette already recorded while spooling the part,
        # BEFORE read() pulls the whole thing into one bytes object -- reading
        # first means a multi-gigabyte part is fully resident in RAM by the
        # time the cap is consulted, which is an OOM, not a rejection.
        if (f.size or 0) > MAX_PHOTO_BYTES:
            frags.append(
                f"<span class='text-safety text-sm'>{html.escape(f.filename or '')} too large (max 20 MB)</span>")
            continue
        data = await f.read()
        if len(data) > MAX_PHOTO_BYTES:
            frags.append(
                f"<span class='text-safety text-sm'>{html.escape(f.filename or '')} too large (max 20 MB)</span>")
            continue
        try:
            ph = photos_svc.save_photo(session, entity_type, entity_id, data, tag=tag)
            session.commit()
            chip = f"<span class='tag absolute bottom-1 left-1'>{tag}</span>" if tag else ""
            frags.append(
                f"<div class='relative h-20 w-20'>"
                f"<img src='/photos/file/{ph.id}?thumb=1' class='h-20 w-20 rounded-lg object-cover'>"
                f"{chip}</div>")
        except ValueError:
            session.rollback()
            frags.append(
                f"<span class='text-safety text-sm'>{html.escape(f.filename or '')} failed — retry</span>")
    return HTMLResponse("".join(frags))

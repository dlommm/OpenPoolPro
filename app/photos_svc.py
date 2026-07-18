import io
import os
import uuid

from PIL import Image, ImageOps

from app import config, models

MAX_LONG_EDGE = 2000
THUMB_LONG_EDGE = 400


def _dir(entity_type: str, entity_id: int) -> str:
    d = os.path.join(config.DATA_DIR, "photos", entity_type, str(entity_id))
    os.makedirs(d, exist_ok=True)
    return d


def save_photo(session, entity_type: str, entity_id: int,
               file_bytes: bytes, caption: str = "") -> models.Photo:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise ValueError("not a decodable image") from e
    d = _dir(entity_type, entity_id)
    stem = uuid.uuid4().hex
    filename, thumb_filename = f"{stem}.jpg", f"{stem}_t.jpg"

    main = img.copy()
    main.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
    main.save(os.path.join(d, filename), "JPEG", quality=85)
    thumb = img.copy()
    thumb.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
    thumb.save(os.path.join(d, thumb_filename), "JPEG", quality=80)

    ph = models.Photo(entity_type=entity_type, entity_id=entity_id,
                      filename=filename, thumb_filename=thumb_filename, caption=caption)
    session.add(ph)
    return ph


def path_for(ph: models.Photo, thumb: bool = False) -> str:
    return os.path.join(config.DATA_DIR, "photos", ph.entity_type, str(ph.entity_id),
                        ph.thumb_filename if thumb else ph.filename)


def delete_photo(session, photo_id: int) -> None:
    ph = session.get(models.Photo, photo_id)
    if ph is None:
        return
    for p in (path_for(ph), path_for(ph, thumb=True)):
        if os.path.exists(p):
            os.remove(p)
    session.delete(ph)

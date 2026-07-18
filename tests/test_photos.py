import io

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def jpeg_bytes(w=3000, h=2000):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "blue").save(buf, format="JPEG")
    return buf.getvalue()


def test_upload_resizes_and_thumbs(client, engine):
    r = client.post("/photos/pool/1",
                    files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
    assert r.status_code == 200
    with Session(engine) as s:
        ph = s.scalars(select(models.Photo)).one()
        assert ph.entity_type == "pool" and ph.entity_id == 1
    orig = client.get(f"/photos/file/{ph.id}")
    assert orig.status_code == 200
    img = Image.open(io.BytesIO(orig.content))
    assert max(img.size) <= 2000
    thumb = client.get(f"/photos/file/{ph.id}?thumb=1")
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= 400


def test_bad_file_does_not_block_good_ones(client, engine):
    r = client.post("/photos/visit/5", files=[
        ("files", ("bad.jpg", b"not an image", "image/jpeg")),
        ("files", ("good.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    assert r.status_code == 200
    with Session(engine) as s:
        assert len(s.scalars(select(models.Photo)).all()) == 1
    assert "failed" in r.text.lower()


def test_delete_photo(client, engine):
    client.post("/photos/pool/1", files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    with Session(engine) as s:
        pid = s.scalars(select(models.Photo)).one().id
    r = client.post(f"/photos/{pid}/delete")
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.scalars(select(models.Photo)).all() == []


def test_bad_entity_type_rejected(client):
    r = client.post("/photos/badtype/1",
                    files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    assert r.status_code == 400


def test_delete_idempotent(client, engine):
    assert client.post("/photos/99999/delete").status_code == 200
    client.post("/photos/pool/1", files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    with Session(engine) as s:
        pid = s.scalars(select(models.Photo)).one().id
    assert client.post(f"/photos/{pid}/delete").status_code == 200
    assert client.post(f"/photos/{pid}/delete").status_code == 200


def exif_rotated_jpeg(w=800, h=600):
    img = Image.new("RGB", (w, h), "red")
    exif = img.getexif()
    exif[274] = 6  # EXIF orientation: rotate 90 CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_exif_orientation_applied(client, engine):
    client.post("/photos/pool/2",
                files=[("files", ("r.jpg", exif_rotated_jpeg(), "image/jpeg"))])
    with Session(engine) as s:
        ph = s.scalars(select(models.Photo).where(
            models.Photo.entity_type == "pool", models.Photo.entity_id == 2)).one()
    img = Image.open(io.BytesIO(client.get(f"/photos/file/{ph.id}").content))
    assert img.size == (600, 800)

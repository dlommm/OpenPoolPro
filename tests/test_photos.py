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


def test_bad_filename_is_escaped_in_error_fragment(client, engine):
    # M1 pin: an uploaded filename is attacker-controlled and lands raw in
    # the error fragment HTML — it must be HTML-escaped.
    r = client.post("/photos/visit/5", files=[
        ("files", ("<img>.jpg", b"not an image", "image/jpeg"))])
    assert r.status_code == 200
    assert "&lt;img&gt;" in r.text
    assert "<img>.jpg" not in r.text


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


def test_upload_with_tag_is_stored(client, engine):
    r = client.post("/photos/pool/1", data={"tag": "before"},
                    files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    assert r.status_code == 200
    with Session(engine) as s:
        ph = s.scalars(select(models.Photo)).one()
        assert ph.tag == "before"
    assert "before" in r.text  # tag chip on the returned fragment


def test_upload_with_no_tag_defaults_to_empty(client, engine):
    client.post("/photos/pool/1", files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    with Session(engine) as s:
        ph = s.scalars(select(models.Photo)).one()
        assert ph.tag == ""


def test_invalid_tag_is_coerced_to_empty(client, engine):
    r = client.post("/photos/pool/1", data={"tag": "bogus"},
                    files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    assert r.status_code == 200
    with Session(engine) as s:
        ph = s.scalars(select(models.Photo)).one()
        assert ph.tag == ""


def _pool_id(client, engine):
    client.post("/customers/new", data={"name": "Filt", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        pid = s.scalars(select(models.Property)).one().id
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "20", "width_ft": "10",
        "avg_depth_ft": "5", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        return s.scalars(select(models.Pool)).one().id


def test_ptag_filter_on_pool_gallery_returns_only_tagged(client, engine):
    pool_id = _pool_id(client, engine)
    client.post(f"/photos/pool/{pool_id}", data={"tag": "before"},
               files=[("files", ("b.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    client.post(f"/photos/pool/{pool_id}", data={"tag": "after"},
               files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    with Session(engine) as s:
        before_id = s.scalars(select(models.Photo).where(models.Photo.tag == "before")).one().id
        after_id = s.scalars(select(models.Photo).where(models.Photo.tag == "after")).one().id

    filtered = client.get(f"/pools/{pool_id}/edit?ptag=before").text
    assert f"/photos/file/{before_id}" in filtered
    assert f"/photos/file/{after_id}" not in filtered


def test_job_photo_tag_chip_renders_on_reload(client, engine):
    with Session(engine) as s:
        c = models.Customer(name="JobCo")
        p = models.Property(customer=c, label="Home")
        s.add(p)
        s.commit()
        pid = p.id
    client.post("/jobs/new", data={
        "property_id": str(pid), "title": "Filter clean", "description": "",
        "priority": "normal", "labor_dollars": "50", "scheduled_date": ""})
    with Session(engine) as s:
        jid = s.scalars(select(models.Job)).one().id
    client.post(f"/photos/job/{jid}", data={"tag": "before"},
               files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    reloaded = client.get(f"/jobs/{jid}").text
    assert "<span class=\"tag absolute bottom-1 left-1\">before</span>" in reloaded


def test_visit_photo_tag_chip_renders_on_reload(client, engine):
    with Session(engine) as s:
        c = models.Customer(name="V")
        p = models.Property(customer=c, frequency="weekly", weekday=0)
        s.add(p)
        s.commit()
        pid = p.id
    client.get(f"/visit/{pid}/2026-07-13")
    with Session(engine) as s:
        vid = s.scalars(select(models.Visit)).one().id
    client.post(f"/photos/visit/{vid}", data={"tag": "issue"},
               files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    reloaded = client.get(f"/visit/{pid}/2026-07-13").text
    assert "<span class=\"tag absolute bottom-1 left-1\">issue</span>" in reloaded


def test_ptag_filter_tolerant_invalid_value_shows_all(client, engine):
    pool_id = _pool_id(client, engine)
    client.post(f"/photos/pool/{pool_id}", data={"tag": "before"},
               files=[("files", ("b.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    client.post(f"/photos/pool/{pool_id}", data={"tag": "after"},
               files=[("files", ("a.jpg", jpeg_bytes(800, 600), "image/jpeg"))])
    with Session(engine) as s:
        before_id = s.scalars(select(models.Photo).where(models.Photo.tag == "before")).one().id
        after_id = s.scalars(select(models.Photo).where(models.Photo.tag == "after")).one().id

    shown = client.get(f"/pools/{pool_id}/edit?ptag=bogus").text
    assert f"/photos/file/{before_id}" in shown
    assert f"/photos/file/{after_id}" in shown

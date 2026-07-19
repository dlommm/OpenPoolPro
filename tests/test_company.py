import io
import os

from PIL import Image
from sqlalchemy.orm import Session

import app.config as config
from app import models


def png_bytes(w=800, h=400):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "#0B2A33").save(buf, format="PNG")
    return buf.getvalue()


def test_company_settings_roundtrip(client, engine):
    r = client.post("/settings/company", data={
        "business_name": "Coastline Pools LLC", "company_address": "1 Dock St",
        "company_phone": "555-0100", "company_email": "info@coastline.example",
        "company_license": "CPC-12345"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert models.get_setting(s, "business_name", "") == "Coastline Pools LLC"
        assert models.get_setting(s, "company_license", "") == "CPC-12345"
    html = client.get("/settings").text
    assert "CPC-12345" in html


def test_logo_upload_serve_remove(client, engine):
    r = client.post("/settings/company/logo",
                    files={"file": ("logo.png", png_bytes(), "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303
    path = os.path.join(config.DATA_DIR, "branding", "logo.png")
    assert os.path.isfile(path)
    img = Image.open(path)
    assert max(img.size) <= 600 and img.format == "PNG"
    r = client.get("/branding/logo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/png")
    client.post("/settings/company/logo/remove")
    assert not os.path.isfile(path)
    assert client.get("/branding/logo").status_code == 404


def test_logo_rejects_garbage_and_oversize(client, engine):
    r = client.post("/settings/company/logo",
                    files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 200 and "logo" in r.text.lower()
    r = client.post("/settings/company/logo",
                    files={"file": ("big.png", b"0" * (2 * 1024 * 1024 + 1), "image/png")})
    assert r.status_code == 200 and "2 MB" in r.text
    path = os.path.join(config.DATA_DIR, "branding", "logo.png")
    assert not os.path.isfile(path)


def test_logo_rejects_non_png_jpeg(client, engine):
    r = client.post("/settings/company/logo",
                    files={"file": ("anim.gif", png_bytes(100, 100), "image/gif")})
    assert r.status_code == 200 and "PNG or JPEG" in r.text
    assert not os.path.isfile(os.path.join(config.DATA_DIR, "branding", "logo.png"))


def test_company_admin_only(client, tech_client):
    t = tech_client(name="nocorp")
    assert t.post("/settings/company", data={"business_name": "X"},
                  follow_redirects=False).status_code == 403
    assert t.get("/branding/logo", follow_redirects=False).status_code in (200, 404)  # serve is open

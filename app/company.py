"""Company identity — the branding settings and logo the PDFs and pages share.

Service-layer home for what used to live in routers/company.py: invoicing,
contract rendering, and pdf_logo all need company_info()/logo_path(), and a
service module importing a router (one even reached for the private
_logo_path) inverted the layering — the invoice engine couldn't be used or
tested without dragging in FastAPI router machinery.
"""
import io
import os

from app import config, models

MAX_LOGO_BYTES = 2 * 1024 * 1024

COMPANY_KEYS = ("business_name", "company_address", "company_phone",
                "company_email", "company_license")


def logo_path() -> str:
    d = os.path.join(config.DATA_DIR, "branding")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "logo.png")


def store_logo(data: bytes, content_type: str | None) -> str | None:
    """Validate and save an uploaded logo; the reason it was refused, or None."""
    from PIL import Image
    if content_type not in ("image/png", "image/jpeg"):
        return "Logo must be a PNG or JPEG"
    if len(data) > MAX_LOGO_BYTES:
        return "Logo must be under 2 MB"
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG"):
            return "Logo must be a PNG or JPEG"
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    except Exception:
        return "That file isn't a readable logo image (PNG/JPEG)"
    img.thumbnail((600, 600))
    img.save(logo_path(), "PNG")
    return None


def company_info(session) -> dict:
    info = {k: models.get_setting(session, k, "") for k in COMPANY_KEYS}
    info["has_logo"] = os.path.isfile(logo_path())
    return info

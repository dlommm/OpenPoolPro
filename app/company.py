"""Company identity — the branding settings and logo the PDFs and pages share.

Service-layer home for what used to live in routers/company.py: invoicing,
contract rendering, and pdf_logo all need company_info()/logo_path(), and a
service module importing a router (one even reached for the private
_logo_path) inverted the layering — the invoice engine couldn't be used or
tested without dragging in FastAPI router machinery.
"""
import os

from app import config, models

COMPANY_KEYS = ("business_name", "company_address", "company_phone",
                "company_email", "company_license")


def logo_path() -> str:
    d = os.path.join(config.DATA_DIR, "branding")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "logo.png")


def company_info(session) -> dict:
    info = {k: models.get_setting(session, k, "") for k in COMPANY_KEYS}
    info["has_logo"] = os.path.isfile(logo_path())
    return info

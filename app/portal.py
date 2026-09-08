"""Customer portal ownership and expiring bearer credentials."""
import hashlib
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app import models


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue(session, customer_id, days=30):
    token = secrets.token_urlsafe(32)
    access = session.get(models.PortalAccess, customer_id)
    if access is None:
        access = models.PortalAccess(customer_id=customer_id)
        session.add(access)
    access.token_hash = digest(token)
    access.expires_at = models.utcnow() + timedelta(days=days)
    access.created_at = models.utcnow()
    return token


def resolve(session, token):
    if not 40 <= len(token) <= 128:
        raise HTTPException(404, "This portal link is unavailable. Ask your service provider for a new link.")
    access = session.scalars(select(models.PortalAccess).where(
        models.PortalAccess.token_hash == digest(token),
        models.PortalAccess.expires_at > models.utcnow())).first()
    customer = session.get(models.Customer, access.customer_id) if access else None
    if customer is None or not customer.active:
        raise HTTPException(404, "This portal link is unavailable. Ask your service provider for a new link.")
    return customer


def public_base(session):
    raw = models.get_setting(session, "public_base_url", "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and parsed.path in ("", "/"))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "Set an HTTPS Public base URL in Settings before creating a portal link.")
    return raw


def photo_query(customer_id):
    props = select(models.Property.id).where(models.Property.customer_id == customer_id)
    visits = select(models.Visit.id).where(models.Visit.property_id.in_(props), models.Visit.status == "done")
    pools = select(models.Pool.id).where(models.Pool.property_id.in_(props))
    jobs = select(models.Job.id).where(models.Job.property_id.in_(props), models.Job.status == "done")
    return select(models.Photo).where(or_(
        and_(models.Photo.entity_type == "property", models.Photo.entity_id.in_(props)),
        and_(models.Photo.entity_type == "visit", models.Photo.entity_id.in_(visits)),
        and_(models.Photo.entity_type == "pool", models.Photo.entity_id.in_(pools)),
        and_(models.Photo.entity_type == "job", models.Photo.entity_id.in_(jobs))))


def safe_file(root, filename):
    root = Path(root).resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "File unavailable")
    return candidate

from builtins import property as builtin_property
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLE_VALUES = ("chlorine_liquid", "chlorine_granular", "chlorine_tab", "ph_up",
               "ph_down", "alkalinity_up", "stabilizer", "salt", "other")

PHOTO_TAGS = ("before", "after", "issue", "house")


def utcnow() -> datetime:
    """Naive UTC now — replacement for deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_tags(raw: str) -> list[str]:
    """Parse a comma-separated tag string into a normalized list.

    Splits on commas, strips whitespace, lowercases, drops empty entries,
    dedupes while preserving order, drops tags longer than 24 characters,
    and caps the result at 10 tags.
    """
    tags: list[str] = []
    for part in raw.split(","):
        tag = part.strip().lower()
        if not tag or len(tag) > 24 or tag in tags:
            continue
        tags.append(tag)
        if len(tags) == 10:
            break
    return tags


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    qb_display_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False)
    # QuickBooks Online link: the Id of the matching QBO Customer once pushed,
    # so a re-sync updates rather than creating a duplicate. Empty = not synced.
    qbo_customer_id: Mapped[str] = mapped_column(String(40), default="")
    qbo_customer_scope: Mapped[str] = mapped_column(String(100), default="")
    properties: Mapped[list["Property"]] = relationship(back_populates="customer")


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    label: Mapped[str] = mapped_column(String(80), default="Home")
    address: Mapped[str] = mapped_column(String(200), default="")
    street: Mapped[str] = mapped_column(String(120), default="")
    city: Mapped[str] = mapped_column(String(80), default="", index=True)
    state: Mapped[str] = mapped_column(String(20), default="")
    zip: Mapped[str] = mapped_column(String(16), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    gate_code: Mapped[str] = mapped_column(String(40), default="")
    dog_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    dog_note: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency: Mapped[str] = mapped_column(String(12), default="weekly")  # weekly|biweekly|every4weeks|none
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0=Mon
    anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    rate_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spa_rate_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_order: Mapped[int] = mapped_column(Integer, default=0)
    assigned_tech_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    customer: Mapped[Customer] = relationship(back_populates="properties")
    pools: Mapped[list["Pool"]] = relationship(back_populates="property")
    assigned_tech: Mapped["User | None"] = relationship(foreign_keys=[assigned_tech_id])


class Pool(Base):
    __tablename__ = "pools"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    label: Mapped[str] = mapped_column(String(80), default="Main pool")
    shape: Mapped[str] = mapped_column(String(12), default="rectangle")  # rectangle|round|oval|kidney
    kind: Mapped[str] = mapped_column(String(8), default="pool")  # pool|spa
    length_ft: Mapped[float] = mapped_column(Float, default=0)  # round: diameter
    width_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_depth_ft: Mapped[float] = mapped_column(Float, default=0)
    gallons: Mapped[int] = mapped_column(Integer, default=0)  # effective (override if set, else computed)
    gallons_override: Mapped[int | None] = mapped_column(Integer, nullable=True)  # manual override of computed gallons
    water_type: Mapped[str] = mapped_column(String(12), default="chlorine")
    surface: Mapped[str] = mapped_column(String(12), default="plaster")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    property: Mapped[Property] = relationship(back_populates="pools")
    equipment: Mapped[list["Equipment"]] = relationship(back_populates="pool")


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    kind: Mapped[str] = mapped_column(String(20), default="pump")
    brand: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    serial: Mapped[str] = mapped_column(String(80), default="")
    install_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    pool: Mapped[Pool] = relationship(back_populates="equipment")


class Photo(Base):
    __tablename__ = "photos"
    # Every photo lookup filters (entity_type, entity_id) — index it so visit
    # forms / job details / customer galleries don't scan the whole table.
    __table_args__ = (Index("ix_photos_entity", "entity_type", "entity_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(10))  # pool|visit|job|property
    entity_id: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(200))
    thumb_filename: Mapped[str] = mapped_column(String(200))
    caption: Mapped[str] = mapped_column(String(200), default="")
    tag: Mapped[str] = mapped_column(String(10), default="")  # before|after|issue|house|""
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SizePreset(Base):
    """Managed library of common pool/spa sizes (seeded defaults + custom).
    Powers the pool-add size picker; dimensions drive computed gallons."""
    __tablename__ = "size_presets"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), default="pool")  # pool|spa
    label: Mapped[str] = mapped_column(String(60))
    shape: Mapped[str] = mapped_column(String(12), default="rectangle")
    length_ft: Mapped[float] = mapped_column(Float, default=0)  # round: diameter
    width_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_depth_ft: Mapped[float] = mapped_column(Float, default=0)
    ref_gallons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # explicit gallons (spas); else computed
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class RateTier(Base):
    __tablename__ = "rate_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    max_gallons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = catch-all
    rate_cents: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(60), default="")
    kind: Mapped[str] = mapped_column(String(8), default="pool")  # pool|spa


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    season_year: Mapped[int] = mapped_column(Integer)
    open_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    weeks: Mapped[int] = mapped_column(Integer, default=16)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    weekly_cents: Mapped[int] = mapped_column(Integer, default=0)
    open_close_cents: Mapped[int] = mapped_column(Integer, default=0)
    green_clean_cents: Mapped[int] = mapped_column(Integer, default=0)
    storm_cents: Mapped[int] = mapped_column(Integer, default=0)
    service_call_cents: Mapped[int] = mapped_column(Integer, default=0)
    spa_cents: Mapped[int] = mapped_column(Integer, default=0)
    includes_spa: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_filename: Mapped[str] = mapped_column(String(200), default="")
    signed_pdf_filename: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Public e-sign link: only the sha256 of the emailed token is stored, so a
    # DB read-out can't mint working sign links. Cleared on signing (one-shot).
    # Indexed: this is the hottest unauthenticated query in the app (every
    # /sign hit) and would otherwise scan the whole contracts table.
    sign_token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    sign_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_name: Mapped[str] = mapped_column(String(120), default="")
    signed_ip: Mapped[str] = mapped_column(String(64), default="")
    # Audit trail for legal defensibility (ESIGN/UETA): when the customer first
    # opened the sign link, and the SHA-256 of the signed PDF so the file can be
    # proven byte-for-byte unaltered later. Retained staff-side, never shown to
    # the customer.
    sign_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_pdf_hash: Mapped[str] = mapped_column(String(64), default="")
    pool: Mapped[Pool] = relationship()


class ContractRate(Base):
    __tablename__ = "contract_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    max_gallons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = catch-all
    label: Mapped[str] = mapped_column(String(60), default="")
    weekly_cents: Mapped[int] = mapped_column(Integer, default=0)
    open_close_cents: Mapped[int] = mapped_column(Integer, default=0)
    green_clean_cents: Mapped[int] = mapped_column(Integer, default=0)
    storm_cents: Mapped[int] = mapped_column(Integer, default=0)
    service_call_cents: Mapped[int] = mapped_column(Integer, default=0)
    spa_cents: Mapped[int] = mapped_column(Integer, default=0)


class Chemical(Base):
    __tablename__ = "chemicals"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(8))  # gal|lb|oz|tab|each
    cost_per_unit_cents: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(20), default="other")
    strength_pct: Mapped[float] = mapped_column(Float, default=100.0)
    markup_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # per-item; None = global markup
    stock_qty: Mapped[float] = mapped_column(Float, default=0.0)
    reorder_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    reorder_qty: Mapped[float | None] = mapped_column(Float, nullable=True)


class Visit(Base):
    __tablename__ = "visits"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(8), default="due", index=True)  # due|done|skipped
    rate_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tax_rate_ppm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labor_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fc: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph: Mapped[float | None] = mapped_column(Float, nullable=True)
    ta: Mapped[float | None] = mapped_column(Float, nullable=True)
    cya: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt: Mapped[float | None] = mapped_column(Float, nullable=True)
    tasks_done: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    original_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    paid_cash: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bumped by a conditional UPDATE on every write that claims this visit.
    # The status check alone is a read-then-write: two handlers that both read
    # status="due" both pass it, and the second silently overwrites the first
    # tech's readings. Comparing-and-swapping this counter makes the loser
    # observable (0 rows matched -> 409) instead of invisible.
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    property: Mapped[Property] = relationship()
    chemicals: Mapped[list["VisitChemical"]] = relationship(back_populates="visit")
    completed_by: Mapped["User | None"] = relationship(foreign_keys=[completed_by_id])

    @builtin_property
    def duration_minutes(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        delta = (self.completed_at - self.started_at).total_seconds()
        return int(delta // 60) if delta >= 0 else None


class VisitChemical(Base):
    __tablename__ = "visit_chemicals"
    id: Mapped[int] = mapped_column(primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    chemical_id: Mapped[int] = mapped_column(ForeignKey("chemicals.id"))
    amount: Mapped[float] = mapped_column(Float)
    unit_cost_cents_frozen: Mapped[int] = mapped_column(Integer)
    total_cost_cents: Mapped[int] = mapped_column(Integer)
    visit: Mapped[Visit] = relationship(back_populates="chemicals")
    chemical: Mapped[Chemical] = relationship()


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    chemical_id: Mapped[int] = mapped_column(ForeignKey("chemicals.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    quantity: Mapped[float] = mapped_column(Float)
    total_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    chemical: Mapped[Chemical] = relationship()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|scheduled|done
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    labor_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_rate_ppm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labor_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_tech_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(10), default="service")
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("maintenance_rules.id"), nullable=True)
    # Which template this job was raised from, when it was. Records the kind of
    # work so billing can post it to a matching QuickBooks item; the free-text
    # title cannot do that job, because an item created from a typo would be
    # permanent (OpenPoolPro never deletes a QuickBooks record).
    job_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_templates.id"), nullable=True)
    job_template: Mapped["JobTemplate | None"] = relationship(foreign_keys=[job_template_id])
    hours: Mapped[float] = mapped_column(Float, default=0.0)
    paid_cash: Mapped[bool] = mapped_column(Boolean, default=False)
    property: Mapped[Property] = relationship()
    assigned_tech: Mapped["User | None"] = relationship(foreign_keys=[assigned_tech_id])
    completed_by: Mapped["User | None"] = relationship(foreign_keys=[completed_by_id])
    tasks: Mapped[list["JobTask"]] = relationship(
        back_populates="job", order_by="JobTask.position",
        cascade="all, delete-orphan")

    @builtin_property
    def duration_minutes(self) -> int | None:
        if self.started_at is None or self.completed_at is None:
            return None
        delta = (self.completed_at - self.started_at).total_seconds()
        return int(delta // 60) if delta >= 0 else None


class TaskTemplate(Base):
    __tablename__ = "task_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CommLog(Base):
    __tablename__ = "comm_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(8))  # call|email|text|note
    note: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ServiceHold(Base):
    __tablename__ = "service_holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    start_date: Mapped[date] = mapped_column(Date)
    resume_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # None = indefinite
    reason: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PropertyTask(Base):
    __tablename__ = "property_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    label: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MaintenanceRule(Base):
    __tablename__ = "maintenance_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    job_type: Mapped[str] = mapped_column(String(10), default="service")  # service|repair
    interval_weeks: Mapped[int] = mapped_column(Integer, default=4)
    lead_days: Mapped[int] = mapped_column(Integer, default=7)
    next_due: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft|sent|accepted|declined|converted
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    lines: Mapped[list["QuoteLine"]] = relationship(back_populates="quote",
                                                     order_by="QuoteLine.sort_order")


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"))
    description: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    quote: Mapped[Quote] = relationship(back_populates="lines")


class JobTemplate(Base):
    __tablename__ = "job_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    job_type: Mapped[str] = mapped_column(String(10), default="service")
    labor_cents: Mapped[int] = mapped_column(Integer, default=0)
    est_hours: Mapped[float] = mapped_column(Float, default=1.0)
    tasks: Mapped[list["JobTemplateTask"]] = relationship(
        back_populates="template", order_by="JobTemplateTask.position",
        cascade="all, delete-orphan")
    prices: Mapped[list["JobTemplatePrice"]] = relationship(
        back_populates="template", order_by="JobTemplatePrice.max_gallons",
        cascade="all, delete-orphan")


class JobTemplatePrice(Base):
    """Per-template size->price row (gallons-keyed, like a rate tier). When a job
    is created from this template, the customer's largest pool gallons pick the row."""
    __tablename__ = "job_template_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_template_id: Mapped[int] = mapped_column(ForeignKey("job_templates.id"))
    label: Mapped[str] = mapped_column(String(60), default="")
    max_gallons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = catch-all
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    hours: Mapped[float] = mapped_column(Float, default=0.0)
    template: Mapped["JobTemplate"] = relationship(back_populates="prices")


class JobTemplateTask(Base):
    __tablename__ = "job_template_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_template_id: Mapped[int] = mapped_column(ForeignKey("job_templates.id"))
    label: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)
    template: Mapped["JobTemplate"] = relationship(back_populates="tasks")


class JobTask(Base):
    __tablename__ = "job_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    label: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    job: Mapped["Job"] = relationship(back_populates="tasks")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(12), default="tech")  # admin|tech
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    username: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    view_money: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_customers: Mapped[bool] = mapped_column(Boolean, default=False)
    can_repairs: Mapped[bool] = mapped_column(Boolean, default=False)
    view_all_routes: Mapped[bool] = mapped_column(Boolean, default=False)
    mark_cash: Mapped[bool] = mapped_column(Boolean, default=False)
    approve_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(200), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)

    PERMISSION_FLAGS = ("view_money", "manage_customers", "can_repairs", "view_all_routes",
                        "mark_cash", "approve_billing")

    def can(self, flag: str) -> bool:
        if self.role == "admin":
            return True
        if flag not in self.PERMISSION_FLAGS:
            return False
        return bool(getattr(self, flag, False))


class TechAbsence(Base):
    __tablename__ = "tech_absences"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    cover_tech_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    cover_tech: Mapped["User | None"] = relationship(foreign_keys=[cover_tech_id])


class RateChange(Base):
    __tablename__ = "rate_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    percent: Mapped[float] = mapped_column(Float)
    scope: Mapped[str] = mapped_column(String(20))  # tiers|overrides|both
    affected_count: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"
    # At most one LIVE invoice per customer per billing period. generate_month's
    # existing-invoice check races when two admins generate concurrently; this
    # index turns the loser's commit into an IntegrityError (caught and counted
    # as skipped) instead of a second bill for the same month. Void invoices are
    # excluded so re-generating after a void still works.
    __table_args__ = (Index("uq_invoices_customer_period_live",
                            "customer_id", "period_year", "period_month",
                            unique=True, sqlite_where=text("status != 'void'")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    subtotal_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    # The SEND lifecycle only. Whether an invoice is paid is DERIVED from its
    # payments (see amount_paid_cents / balance_cents) rather than stored here,
    # so a stored status can never disagree with the money actually recorded.
    status: Mapped[str] = mapped_column(String(8), default="draft")  # draft|sent|void
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pdf_filename: Mapped[str] = mapped_column(String(200))
    customer: Mapped[Customer] = relationship()
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan")

    @property
    def amount_paid_cents(self) -> int:
        return sum(p.amount_cents for p in self.payments)

    @property
    def balance_cents(self) -> int:
        """What is still owed. Void invoices owe nothing."""
        if self.status == "void":
            return 0
        return self.total_cents - self.amount_paid_cents

    @property
    def payment_state(self) -> str:
        """unpaid | partial | paid | void — derived, never stored."""
        if self.status == "void":
            return "void"
        paid = self.amount_paid_cents
        if paid <= 0:
            return "unpaid"
        return "paid" if paid >= self.total_cents else "partial"

    def days_overdue(self, today: date) -> int:
        """0 when not yet due, fully paid, or void."""
        if self.due_date is None or self.balance_cents <= 0:
            return 0
        return max(0, (today - self.due_date).days)


PAYMENT_METHODS = ("check", "cash", "card", "ach", "other")


class Payment(Base):
    """Money actually received against an invoice.

    Kept as rows rather than a paid flag so partial payments, a bounced check
    backed out, and the audit question "when did this arrive and how?" all have
    real answers.
    """
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    received_on: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(10), default="check")
    reference: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    # Text, not String(N): the contract_template key stores multi-KB JSON.
    # SQLite never enforced the old String(200) so nothing broke, but the
    # declared type should not lie to the next reader (or to a future
    # Postgres port, which WOULD enforce it).
    value: Mapped[str] = mapped_column(Text)


# Settings rows holding credentials. These are encrypted at rest (AES-256-GCM,
# key derived from SECRET_KEY) transparently by get_setting/set_setting, so
# every existing call site is covered and none can forget. The threat this
# closes is a leaked DATABASE/backup — the nightly backup ships the DB off-box
# but never the environment, so ciphertext alone is useless. See app/secretbox.
ENCRYPTED_SETTING_KEYS = frozenset({
    "smtp_pass",                  # SMTP password
    "smtp_oauth_refresh_token",   # Office 365 refresh token
    "qbo_client_secret",          # QuickBooks app secret
    "qbo_refresh_token",          # QuickBooks refresh token
    "geoapify_key",               # Geoapify API key
    "labcom_token",               # LabCOM cloud API token (PoolLab testers)
    "ondilo_refresh_token",       # Ondilo ICO OAuth refresh token
})


def get_setting(session, key: str, default: str) -> str:
    row = session.get(Setting, key)
    if row is None:
        return default
    if key in ENCRYPTED_SETTING_KEYS:
        from app import secretbox
        return secretbox.decrypt(row.value)
    return row.value


def set_setting(session, key: str, value: str) -> None:
    if key in ENCRYPTED_SETTING_KEYS:
        from app import secretbox
        value = secretbox.encrypt(value)
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


class TaxRate(Base):
    __tablename__ = "tax_rates"
    state: Mapped[str] = mapped_column(String(2), primary_key=True)  # uppercase 2-letter
    rate_ppm: Mapped[int] = mapped_column(Integer, default=0)
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)


class BackupCode(Base):
    __tablename__ = "backup_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")


def audit(session, user_id: int | None, action: str, detail: str = "") -> AuditLog:
    """Record a sensitive-action audit row. Caller commits."""
    row = AuditLog(user_id=user_id, action=action, detail=detail, at=utcnow())
    session.add(row)
    session.flush()
    return row


def claim_contract_status(session, contract: Contract,
                          expected: tuple[str, ...], new_status: str) -> bool:
    """Atomically transition contract.status with a conditional UPDATE.

    Every lifecycle route reads the row, checks status in Python, then writes
    — a check-then-act window in which a concurrent transition can be silently
    overwritten (a customer e-sign racing a staff void was the concrete case).
    The UPDATE ... WHERE status IN expected makes the loser observe rowcount 0
    instead of clobbering the winner; SQLite's write lock plus the engine's 5s
    busy timeout serialize the writers. Runs inside the caller's open
    transaction — on False the caller must abandon the request without
    committing (session teardown rolls the claim attempt back)."""
    from sqlalchemy import update

    result = session.execute(
        update(Contract)
        .where(Contract.id == contract.id, Contract.status.in_(expected))
        .values(status=new_status))
    if result.rowcount != 1:
        return False
    contract.status = new_status  # keep the in-memory instance in sync
    return True


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    to_addr: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    # Branded HTML alternative ("" = plain text only); see app.email_brand.
    html: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(8), default="pending")  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PortalAccess(Base):
    __tablename__ = "portal_access"
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PortalPhoto(Base):
    __tablename__ = "portal_photos"
    photo_id: Mapped[int] = mapped_column(ForeignKey("photos.id"), primary_key=True)


class InvoiceFollowup(Base):
    __tablename__ = "invoice_followups"
    __table_args__ = (Index("uq_followup_stage", "invoice_id", "stage", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    stage: Mapped[int] = mapped_column(Integer)
    balance_cents: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    outbox_id: Mapped[int] = mapped_column(ForeignKey("email_outbox.id"), unique=True)
    approved_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    outbox: Mapped[EmailOutbox] = relationship()
    invoice: Mapped[Invoice] = relationship()


class WaterReview(Base):
    __tablename__ = "water_reviews"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OperationRun(Base):
    __tablename__ = "operation_runs"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="running")


class QBODraft(Base):
    __tablename__ = "qbo_billing_drafts"
    __table_args__ = (Index("uq_qbo_draft_period", "customer_id", "period_year", "period_month", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    scope: Mapped[str] = mapped_column(String(100))
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    source_keys: Mapped[list] = mapped_column(JSON, default=list)
    email: Mapped[str] = mapped_column(String(120), default="")
    due_date: Mapped[date] = mapped_column(Date)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    approved_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    request_id: Mapped[str] = mapped_column(String(50), default="")
    qbo_invoice_id: Mapped[str] = mapped_column(String(40), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    customer: Mapped[Customer] = relationship()


class QBOInvoiceMirror(Base):
    __tablename__ = "qbo_invoice_mirrors"
    __table_args__ = (Index("uq_qbo_invoice_scope", "scope", "qbo_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(100))
    qbo_id: Mapped[str] = mapped_column(String(40))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    number: Mapped[str] = mapped_column(String(100), default="")
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid_cents: Mapped[int] = mapped_column(Integer, default=0)
    allocation_known: Mapped[bool] = mapped_column(Boolean, default=True)
    credit_cents: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="unpaid")
    email_status: Mapped[str] = mapped_column(String(30), default="")
    payment_link: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    customer: Mapped[Customer] = relationship()


class QBOTransactionMirror(Base):
    __tablename__ = "qbo_transaction_mirrors"
    __table_args__ = (Index("uq_qbo_transaction_scope", "scope", "kind", "qbo_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    qbo_id: Mapped[str] = mapped_column(String(40))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QBOItemMap(Base):
    """One OpenPoolPro billing kind -> one QuickBooks item, per connection.

    Replaces the pair of settings rows that could only ever name two items.
    `item_key` is the stable kind ("service", "chemical:4", "template:2"),
    never a free-text label, so a rename in OpenPoolPro cannot strand the
    mapping or provoke a second permanent item in QuickBooks.
    """

    __tablename__ = "qbo_item_map"
    __table_args__ = (UniqueConstraint("scope", "item_key", name="uq_qbo_item_map_scope_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    item_key: Mapped[str] = mapped_column(String(80))
    qbo_item_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QBOBillingSource(Base):
    """A source remains reserved even if its completion date is subsequently edited."""
    __tablename__ = "qbo_billing_sources"
    source_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("qbo_billing_drafts.id"), index=True)


# --- Water testers ------------------------------------------------------------
# Readings arrive from handheld testers (LabCOM cloud: PoolLab / PoolWaterLAB /
# PrimeLab), in-pool monitors (Ondilo ICO, iopool) and exported files (eXact
# iDip, Hanna Lab, LaMotte WaterLink Connect, anything CSV). None of them know
# which customer they belong to, so every reading lands here first and is tied
# to a pool only by an explicit TesterLink, a tech choosing it on that pool's
# visit, or an office user assigning it. See app/water_readings.py.

class TesterDevice(Base):
    """A physical tester, auto-registered the first time its serial is seen.
    Assigning it to a tech is what lets that tech pull its unassigned readings
    onto a visit — nobody else's tester shows up on their visit form."""
    __tablename__ = "tester_devices"
    __table_args__ = (UniqueConstraint("source", "serial", name="uq_tester_devices_source_serial"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    serial: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped["User | None"] = relationship()


class TesterLink(Base):
    """An external account/pool/label that always means one of our pools:
    a LabCOM account, an Ondilo or iopool pool, or a customer name as it
    appears in an imported file."""
    __tablename__ = "tester_links"
    __table_args__ = (UniqueConstraint("source", "external_key", name="uq_tester_links_source_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    external_key: Mapped[str] = mapped_column(String(160))
    external_label: Mapped[str] = mapped_column(String(200), default="")
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    # iopool issues one API key per homeowner account; it is stored with the
    # pool it reads, encrypted like every other credential (app/secretbox).
    secret: Mapped[str] = mapped_column(Text, default="")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    property: Mapped[Property] = relationship()


class DeviceReading(Base):
    """One water test: every parameter measured in one sitting.

    property_id is None until the reading is tied to a pool; visit_id is set
    once a completed visit used it (UNIQUE: a reading fills one visit, ever).
    `edited` records that the tech changed a value before saving, so the
    visit's provenance never claims a number the tester did not produce."""
    __tablename__ = "device_readings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_device_readings_source_ext"),
        UniqueConstraint("visit_id", name="uq_device_readings_visit"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))            # labcom|ondilo|iopool|file
    kind: Mapped[str] = mapped_column(String(8), default="handheld")  # handheld|monitor
    external_id: Mapped[str] = mapped_column(String(200))
    device_serial: Mapped[str] = mapped_column(String(80), default="")
    external_key: Mapped[str] = mapped_column(String(160), default="")
    external_label: Mapped[str] = mapped_column(String(200), default="")
    measured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"), nullable=True, index=True)
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"), nullable=True)
    match_method: Mapped[str] = mapped_column(String(8), default="")  # link|visit|manual
    status: Mapped[str] = mapped_column(String(8), default="new")     # new|ignored
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    property: Mapped["Property | None"] = relationship()
    visit: Mapped["Visit | None"] = relationship()

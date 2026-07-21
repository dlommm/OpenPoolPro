from builtins import property as builtin_property
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLE_VALUES = ("chlorine_liquid", "chlorine_granular", "chlorine_tab", "ph_up",
               "ph_down", "alkalinity_up", "stabilizer", "salt", "other")

PHOTO_TAGS = ("before", "after", "issue")


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
    name: Mapped[str] = mapped_column(String(120))
    qb_display_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False)
    properties: Mapped[list["Property"]] = relationship(back_populates="customer")


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    label: Mapped[str] = mapped_column(String(80), default="Home")
    address: Mapped[str] = mapped_column(String(200), default="")
    gate_code: Mapped[str] = mapped_column(String(40), default="")
    dog_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    dog_note: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency: Mapped[str] = mapped_column(String(12), default="weekly")  # weekly|biweekly|every4weeks|none
    weekday: Mapped[int] = mapped_column(Integer, default=0)  # 0=Mon
    anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    rate_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    length_ft: Mapped[float] = mapped_column(Float, default=0)  # round: diameter
    width_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_depth_ft: Mapped[float] = mapped_column(Float, default=0)
    gallons: Mapped[int] = mapped_column(Integer, default=0)
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
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(10))  # pool|visit|job
    entity_id: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(200))
    thumb_filename: Mapped[str] = mapped_column(String(200))
    caption: Mapped[str] = mapped_column(String(200), default="")
    tag: Mapped[str] = mapped_column(String(10), default="")  # before|after|issue|""
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RateTier(Base):
    __tablename__ = "rate_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    max_gallons: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = catch-all
    rate_cents: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(60), default="")


class Chemical(Base):
    __tablename__ = "chemicals"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(8))  # gal|lb|oz|tab|each
    cost_per_unit_cents: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(20), default="other")
    strength_pct: Mapped[float] = mapped_column(Float, default=100.0)
    stock_qty: Mapped[float] = mapped_column(Float, default=0.0)
    reorder_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    reorder_qty: Mapped[float | None] = mapped_column(Float, nullable=True)


class Visit(Base):
    __tablename__ = "visits"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8), default="due")  # due|done|skipped
    rate_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fc: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph: Mapped[float | None] = mapped_column(Float, nullable=True)
    ta: Mapped[float | None] = mapped_column(Float, nullable=True)
    cya: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt: Mapped[float | None] = mapped_column(Float, nullable=True)
    tasks_done: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    original_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|scheduled|done
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    labor_cents: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_tech_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(10), default="service")
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("maintenance_rules.id"), nullable=True)
    property: Mapped[Property] = relationship()
    assigned_tech: Mapped["User | None"] = relationship(foreign_keys=[assigned_tech_id])
    completed_by: Mapped["User | None"] = relationship(foreign_keys=[completed_by_id])

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
    totp_secret: Mapped[str | None] = mapped_column(String(200), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)

    PERMISSION_FLAGS = ("view_money", "manage_customers", "can_repairs", "view_all_routes")

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
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    period_year: Mapped[int] = mapped_column(Integer)
    period_month: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="draft")  # draft|sent|void
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pdf_filename: Mapped[str] = mapped_column(String(200))
    customer: Mapped[Customer] = relationship()


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


def get_setting(session, key: str, default: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


class BackupCode(Base):
    __tablename__ = "backup_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")


def audit(session, user_id: int | None, action: str, detail: str = "") -> AuditLog:
    """Record a sensitive-action audit row. Caller commits."""
    row = AuditLog(user_id=user_id, action=action, detail=detail, at=utcnow())
    session.add(row)
    session.flush()
    return row


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    to_addr: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), default="pending")  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

"""Customer sync planning: decide what WOULD change before anything does.

OpenPoolPro is the system of record and QuickBooks is downstream, so every
customer write is planned from reads first, shown to an operator, and applied
only in the shape that was displayed.

Two rules shape everything here:

* Additive writes (a field QuickBooks holds empty) destroy nothing and apply on
  their own. Overwrites -- and every display-name change, because the name is
  QuickBooks' unique key -- wait for explicit approval.
* An approval binds to a plan digest. The plan is recomputed at apply time and
  the digest re-checked, so a customer edited in QuickBooks between the review
  screen and the click is never written with stale intent. This is the same
  discipline qbo_billing applies to an approved invoice before delivery.

Nothing in this module deletes, deactivates or merges a QuickBooks record; the
transport in qbo_api refuses to express those at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select

from app import models, qbo_api

# Below this, two names are unrelated. Above it they are shown as a possible
# match for a human to judge -- never linked automatically.
NEAR_MATCH_RATIO = 0.85


class SyncError(ValueError):
    pass


def _norm(value) -> str:
    """Comparison form for a customer name: case and spacing carry no meaning."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _local_display(customer) -> str:
    return (customer.qb_display_name or customer.name or "").strip()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


@dataclass
class FieldChange:
    field: str          # QuickBooks field name
    label: str          # operator-facing label
    old: str            # what QuickBooks holds now ("" when empty)
    new: str            # what OpenPoolPro holds
    additive: bool      # True when QuickBooks is empty -- nothing is destroyed

    @property
    def key(self) -> str:
        return self.field


@dataclass
class CustomerPlanRow:
    customer_id: int
    name: str
    qbo_id: str = ""
    remote_name: str = ""
    remote_email: str = ""
    changes: list[FieldChange] = field(default_factory=list)
    ratio: float = 0.0
    reason: str = ""

    @property
    def key(self) -> str:
        return f"cust:{self.customer_id}"

    @property
    def additive(self) -> list[FieldChange]:
        return [c for c in self.changes if c.additive]

    @property
    def overwriting(self) -> list[FieldChange]:
        return [c for c in self.changes if not c.additive]


# ---------- push: OpenPoolPro -> QuickBooks ----------

def plan_push(session) -> dict:
    """What syncing customers to QuickBooks would do. Reads only.

    Returns creates, exact-name links, near-match reviews, additive updates that
    apply on their own, and overwriting updates that need approval.
    """
    scope = qbo_api.connection_scope(session)
    remote = qbo_api.query_customers(session)
    by_id = {str(r.get("Id")): r for r in remote if r.get("Id")}
    by_name: dict[str, dict] = {}
    for raw in remote:
        by_name.setdefault(_norm(raw.get("DisplayName")), raw)

    creates: list[CustomerPlanRow] = []
    links: list[CustomerPlanRow] = []
    review: list[CustomerPlanRow] = []
    additive: list[CustomerPlanRow] = []
    approval: list[CustomerPlanRow] = []
    unchanged = 0
    stale = 0

    locals_ = session.scalars(
        select(models.Customer).where(models.Customer.active)
        .order_by(models.Customer.id)).all()

    for customer in locals_:
        display = _local_display(customer)
        row = CustomerPlanRow(customer_id=customer.id, name=display)

        if customer.qbo_customer_id and customer.qbo_customer_scope == scope:
            raw = by_id.get(str(customer.qbo_customer_id))
            if raw is None:
                # Linked to an id this company does not return. Never re-create
                # under a new id from a sync -- that is how duplicates appear.
                row.reason = "linked to a QuickBooks customer this company did not return"
                row.qbo_id = str(customer.qbo_customer_id)
                stale += 1
                review.append(row)
                continue
            row.qbo_id = str(raw.get("Id"))
            row.remote_name = str(raw.get("DisplayName", ""))
            row.changes = _field_changes(customer, raw)
            if not row.changes:
                unchanged += 1
            elif row.overwriting:
                approval.append(row)
            else:
                additive.append(row)
            continue

        if customer.qbo_customer_id and customer.qbo_customer_scope != scope:
            row.reason = "carries a customer id from a different QuickBooks company"
            review.append(row)
            continue

        exact = by_name.get(_norm(display))
        if exact is not None:
            # Adopt before create: QuickBooks enforces unique display names, so
            # creating here would fail -- and a silent failure count teaches an
            # operator nothing about the customer already sitting in QuickBooks.
            row.qbo_id = str(exact.get("Id"))
            row.remote_name = str(exact.get("DisplayName", ""))
            row.remote_email = qbo_api.remote_customer_value(exact, "PrimaryEmailAddr")
            links.append(row)
            continue

        near = _best_near_match(display, customer.email, remote)
        if near is not None:
            raw, ratio = near
            row.qbo_id = str(raw.get("Id"))
            row.remote_name = str(raw.get("DisplayName", ""))
            row.remote_email = qbo_api.remote_customer_value(raw, "PrimaryEmailAddr")
            row.ratio = round(ratio, 3)
            row.reason = "looks like an existing QuickBooks customer"
            review.append(row)
            continue

        creates.append(row)

    plan = {
        "scope": scope,
        "creates": creates,
        "links": links,
        "review": review,
        "additive": additive,
        "approval": approval,
        "unchanged": unchanged,
        "stale": stale,
    }
    plan["digest"] = plan_digest(plan)
    return plan


def _field_changes(customer, raw: dict) -> list[FieldChange]:
    """Per-field diff between the local customer and its QuickBooks record.

    A blank local value is never pushed: OpenPoolPro not knowing a phone number
    is not a statement that QuickBooks' phone number is wrong.
    """
    wanted = {
        "DisplayName": _local_display(customer),
        "PrimaryEmailAddr": (customer.email or "").strip(),
        "PrimaryPhone": (customer.phone or "").strip(),
    }
    changes = []
    for qbo_field, label in qbo_api.CUSTOMER_FIELDS.items():
        new = wanted[qbo_field]
        if not new:
            continue
        old = qbo_api.remote_customer_value(raw, qbo_field)
        if old.strip() == new:
            continue
        changes.append(FieldChange(
            field=qbo_field, label=label, old=old, new=new,
            # The display name is QuickBooks' unique key for the record, so a
            # change to it is never treated as additive even from empty.
            additive=(not old.strip()) and qbo_field != "DisplayName"))
    return changes


def _best_near_match(display: str, email: str, remote: list[dict]):
    """Closest plausible QuickBooks customer, or None.

    A shared non-empty email is strong evidence of the same person even when
    the names differ ("E Vance" / "Ed Vance"), so it scores as a match on its
    own. Otherwise names must be close enough to be worth a human's attention.
    """
    email = (email or "").strip().casefold()
    best, best_ratio = None, 0.0
    for raw in remote:
        remote_email = qbo_api.remote_customer_value(raw, "PrimaryEmailAddr").strip().casefold()
        if email and remote_email and email == remote_email:
            return raw, 1.0
        ratio = _similar(display, raw.get("DisplayName", ""))
        if ratio > best_ratio:
            best, best_ratio = raw, ratio
    if best is not None and best_ratio >= NEAR_MATCH_RATIO:
        return best, best_ratio
    return None


def plan_digest(plan: dict) -> str:
    """Stable identity for a plan's decisions.

    Approval is bound to this. Counts alone would not do: two plans can agree on
    totals and disagree on which customer gets which value.
    """
    def rows_with_changes(bucket):
        # Keyed by customer id and sorted by field so the digest depends on the
        # decisions themselves, never on iteration order.
        return sorted(
            [[r.customer_id, sorted([asdict(c) for c in r.changes], key=lambda c: c["field"])]
             for r in plan[bucket]],
            key=lambda pair: pair[0])

    shape = {
        "scope": plan["scope"],
        "creates": sorted(r.customer_id for r in plan["creates"]),
        "links": sorted((r.customer_id, r.qbo_id) for r in plan["links"]),
        "review": sorted((r.customer_id, r.qbo_id, r.reason) for r in plan["review"]),
        "additive": rows_with_changes("additive"),
        "approval": rows_with_changes("approval"),
    }
    return _digest(shape)


def apply_push(session, digest: str, approved: set[str], user_id=None) -> dict:
    """Apply a reviewed push plan.

    The plan is recomputed here rather than trusted from the form: the operator
    approved a specific set of changes against a specific QuickBooks state, and
    that state is only knowable now. A mismatch stops everything rather than
    writing a stale intent.
    """
    scope = qbo_api.connection_scope(session)
    with qbo_api.connection_guard(session, scope):
        plan = plan_push(session)
        if plan["digest"] != digest:
            raise SyncError(
                "Customers changed in OpenPoolPro or QuickBooks since this review. "
                "Nothing was written. Review the updated plan and try again.")

        created = linked = updated = skipped = 0
        errors: list[str] = []

        for row in plan["links"]:
            customer = session.get(models.Customer, row.customer_id)
            # An exact display-name match is the same record by QuickBooks'
            # own uniqueness rule; linking writes nothing remotely.
            customer.qbo_customer_id = row.qbo_id
            customer.qbo_customer_scope = scope
            session.commit()
            linked += 1

        for row in plan["creates"]:
            customer = session.get(models.Customer, row.customer_id)
            try:
                customer.qbo_customer_id = qbo_api.create_customer(session, customer)
                session.commit()
                created += 1
            except qbo_api.QboError as exc:
                session.rollback()
                errors.append(f"{row.name}: {exc}")

        for row in plan["additive"] + plan["approval"]:
            customer = session.get(models.Customer, row.customer_id)
            fields = {c.field: c.new for c in row.additive}
            for change in row.overwriting:
                if f"{row.key}:{change.key}" in approved:
                    fields[change.field] = change.new
                else:
                    skipped += 1
            if not fields:
                continue
            try:
                qbo_api.update_customer_fields(session, customer, fields)
                session.commit()
                updated += 1
            except qbo_api.QboError as exc:
                session.rollback()
                errors.append(f"{row.name}: {exc}")

        models.audit(session, user_id, "settings.qbo",
                     detail=(f"customer push: {created} created, {linked} linked, "
                             f"{updated} updated, {skipped} awaiting approval"))
        session.commit()

    return {"created": created, "linked": linked, "updated": updated,
            "skipped": skipped, "errors": errors,
            "review": len(plan["review"])}


# ---------- import: QuickBooks -> OpenPoolPro ----------

@dataclass
class ImportRow:
    qbo_id: str
    name: str
    email: str = ""
    phone: str = ""
    customer_id: int | None = None   # local match, when there is one
    local_name: str = ""
    ratio: float = 0.0

    @property
    def key(self) -> str:
        return f"qbo:{self.qbo_id}"


def plan_import(session) -> dict:
    """What importing QuickBooks customers into OpenPoolPro would do. Reads only.

    Nothing here writes to QuickBooks at all -- import is a pure read of the
    remote roster followed by local inserts and links.
    """
    scope = qbo_api.connection_scope(session)
    remote = qbo_api.query_customers(session)
    locals_ = session.scalars(select(models.Customer).order_by(models.Customer.id)).all()

    linked_ids = {c.qbo_customer_id for c in locals_
                  if c.qbo_customer_id and c.qbo_customer_scope == scope}
    unlinked = [c for c in locals_ if not c.qbo_customer_id]
    by_name: dict[str, object] = {}
    for customer in unlinked:
        by_name.setdefault(_norm(_local_display(customer)), customer)

    auto_link: list[ImportRow] = []
    creates: list[ImportRow] = []
    review: list[ImportRow] = []
    already = 0
    claimed: set[int] = set()

    for raw in remote:
        qbo_id = str(raw.get("Id", ""))
        if not qbo_id:
            continue
        if qbo_id in linked_ids:
            already += 1
            continue
        name = str(raw.get("DisplayName", "")).strip()
        row = ImportRow(qbo_id=qbo_id, name=name,
                        email=qbo_api.remote_customer_value(raw, "PrimaryEmailAddr"),
                        phone=qbo_api.remote_customer_value(raw, "PrimaryPhone"))

        exact = by_name.get(_norm(name))
        if exact is not None and exact.id not in claimed:
            row.customer_id, row.local_name = exact.id, _local_display(exact)
            claimed.add(exact.id)
            auto_link.append(row)
            continue

        near = _best_local_match(name, row.email, unlinked, claimed)
        if near is not None:
            customer, ratio = near
            row.customer_id, row.local_name = customer.id, _local_display(customer)
            row.ratio = round(ratio, 3)
            review.append(row)
            continue

        creates.append(row)

    plan = {"scope": scope, "auto_link": auto_link, "creates": creates,
            "review": review, "already": already}
    plan["digest"] = import_digest(plan)
    return plan


def _best_local_match(name: str, email: str, candidates, claimed: set[int]):
    email = (email or "").strip().casefold()
    best, best_ratio = None, 0.0
    for customer in candidates:
        if customer.id in claimed:
            continue
        if email and (customer.email or "").strip().casefold() == email:
            return customer, 1.0
        ratio = _similar(name, _local_display(customer))
        if ratio > best_ratio:
            best, best_ratio = customer, ratio
    if best is not None and best_ratio >= NEAR_MATCH_RATIO:
        return best, best_ratio
    return None


def import_digest(plan: dict) -> str:
    shape = {
        "scope": plan["scope"],
        "auto_link": sorted((r.qbo_id, r.customer_id) for r in plan["auto_link"]),
        "creates": sorted((r.qbo_id, r.name) for r in plan["creates"]),
        "review": sorted((r.qbo_id, r.customer_id) for r in plan["review"]),
    }
    return _digest(shape)


def apply_import(session, digest: str, decisions: dict, user_id=None) -> dict:
    """Apply a reviewed import plan.

    `decisions` maps a review row key to "link" or "create"; anything else is
    skipped. Recomputed and digest-checked for the same reason as apply_push.
    """
    scope = qbo_api.connection_scope(session)
    with qbo_api.connection_guard(session, scope):
        plan = plan_import(session)
        if plan["digest"] != digest:
            raise SyncError(
                "Customers changed in OpenPoolPro or QuickBooks since this review. "
                "Nothing was imported. Review the updated plan and try again.")

        created = linked = skipped = 0

        def link(row):
            customer = session.get(models.Customer, row.customer_id)
            if customer is None or customer.qbo_customer_id:
                return False
            customer.qbo_customer_id = row.qbo_id
            customer.qbo_customer_scope = scope
            if not customer.qb_display_name and row.name != customer.name:
                # Keep QuickBooks' own display name so a later push sees no
                # difference and plans no rename of the remote record.
                customer.qb_display_name = row.name
            return True

        def create(row):
            session.add(models.Customer(
                name=row.name, qb_display_name=row.name, email=row.email, phone=row.phone,
                active=True, qbo_customer_id=row.qbo_id, qbo_customer_scope=scope))
            return True

        for row in plan["auto_link"]:
            linked += 1 if link(row) else 0
        for row in plan["creates"]:
            created += 1 if create(row) else 0
        for row in plan["review"]:
            choice = decisions.get(row.key, "skip")
            if choice == "link":
                linked += 1 if link(row) else 0
            elif choice == "create":
                created += 1 if create(row) else 0
            else:
                skipped += 1

        models.audit(session, user_id, "settings.qbo",
                     detail=f"customer import: {created} created, {linked} linked, {skipped} skipped")
        session.commit()

    return {"created": created, "linked": linked, "skipped": skipped}


# ---------- billing items ----------
#
# A billing line names the KIND of work it is, not a label. The kind is stable;
# a label is not. That distinction matters more here than it normally would,
# because OpenPoolPro never deletes a QuickBooks record -- an item created from
# an edited job title would be permanent clutter in someone's chart of accounts.

def item_key_for_visit() -> tuple[str, str]:
    return "service", qbo_api.DEFAULT_ITEM_NAMES["service"]


def item_key_for_chemical(chemical) -> tuple[str, str]:
    return f"chemical:{chemical.id}", (chemical.name or "").strip()


def item_key_for_job(job) -> tuple[str, str]:
    """Template first, job type second. Never the free-text title."""
    template = getattr(job, "job_template", None)
    if job.job_template_id and template is not None and (template.title or "").strip():
        return f"template:{job.job_template_id}", template.title.strip()
    if job.job_type == "repair":
        return "jobtype:repair", qbo_api.DEFAULT_ITEM_NAMES["repair"]
    return "service", qbo_api.DEFAULT_ITEM_NAMES["service"]


def mapped_item(session, item_key: str) -> str:
    """The QuickBooks item id already recorded for this kind, or ''."""
    row = session.scalar(
        select(models.QBOItemMap).where(
            models.QBOItemMap.scope == qbo_api.connection_scope(session),
            models.QBOItemMap.item_key == item_key))
    return row.qbo_item_id if row else ""


def income_account(session) -> str:
    account = models.get_setting(session, "qbo_income_account_id", "")
    if not account:
        raise SyncError(
            "Choose the QuickBooks income account new billing items should post to, once, "
            "in Settings → QuickBooks Online.")
    return account


def resolve_item(session, item_key: str, name: str) -> str:
    """The QuickBooks item for a billing kind, creating it if it is missing.

    Adopt before create, for the same reason everywhere else does: QuickBooks
    enforces unique item names, and a name already in use is the operator's
    existing item for this work, not an obstacle. An existing name of a type an
    invoice line cannot safely reference is refused rather than mapped -- that
    would surface as a billing mismatch on a real invoice instead of here.

    Creating is additive and so needs no approval of its own; the operator
    approves the whole draft, and the draft review lists what will be created.
    """
    existing = mapped_item(session, item_key)
    if existing:
        return existing
    name = (name or "").strip()
    if not name:
        raise SyncError("A billing item needs a name before it can be created in QuickBooks.")
    scope = qbo_api.connection_scope(session)
    remote = qbo_api.find_item_by_name(session, name)
    if remote is not None:
        if remote["type"] not in qbo_api.SAFE_ITEM_TYPES:
            raise SyncError(
                f'"{name}" already exists in QuickBooks as a {remote["type"]} item, which an '
                "invoice line cannot safely use. Rename it in QuickBooks, or rename the "
                "template or chemical it came from.")
        if not remote["active"]:
            raise SyncError(
                f'"{name}" exists in QuickBooks but is inactive. Reactivate it there, or '
                "rename the template or chemical it came from.")
        item_id = remote["id"]
    else:
        item_id = qbo_api.create_service_item(session, name, income_account(session))["id"]
    session.add(models.QBOItemMap(scope=scope, item_key=item_key, qbo_item_id=item_id, name=name))
    # Committed now, not with whatever transaction happens to be open. The item
    # exists in QuickBooks from this point and cannot be removed; if approval
    # fails a moment later, the mapping must still be here so the retry reuses
    # it rather than re-deriving it.
    session.commit()
    return item_id


def missing_items(session, lines) -> list[tuple[str, str]]:
    """(key, name) for every billing kind in `lines` with no item yet.

    Read-only: the draft review calls this so an approver sees what approving
    would create in QuickBooks before they approve it.
    """
    out, seen = [], set()
    for line in lines:
        key, name = line.get("item_key", ""), line.get("item_name", "")
        if not key or key in seen:
            continue
        seen.add(key)
        if not mapped_item(session, key):
            out.append((key, name))
    return out

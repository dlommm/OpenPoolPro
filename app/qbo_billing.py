"""Reviewed billing commands and a locally retained QuickBooks financial mirror.

Every network mutation has a committed state before it starts. Invoice creation
reuses one request ID and immutable payload; uncertain delivery is never retried.
Financial mirrors are separate from legacy invoices so credits cannot masquerade
as received cash and existing local payments cannot be counted twice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app import models, qbo, qbo_api, receivables, tax


class BillingError(ValueError):
    pass


def _scope(session):
    scope = qbo_api.connection_scope(session)
    if not models.get_setting(session, "qbo_realm_id", ""):
        raise BillingError("Connect QuickBooks before preparing billing.")
    return scope


def _validate_sources(session, row):
    for key in row.source_keys:
        kind, source_id = key.split(":", 1)
        entity = models.Visit if kind == "visit" else models.Job
        source = session.get(entity, int(source_id))
        claim = session.get(models.QBOBillingSource, key)
        if (
            source is None
            or source.status != "done"
            or source.paid_cash
            or source.property.customer_id != row.customer_id
            or claim is None
            or claim.draft_id != row.id
        ):
            raise BillingError(
                "Source work changed, was paid in cash, or is no longer billable. "
                "Review the source work before billing."
            )


def _enabled(session):
    if models.get_setting(session, "qbo_billing_enabled", "0") != "1":
        raise BillingError("Enable reviewed QuickBooks billing in billing settings first.")
    if models.get_setting(session, "qbo_billing_scope", "") != _scope(session):
        raise BillingError("Confirm billing settings for the currently connected QuickBooks company.")


def _cents(value):
    try:
        amount = Decimal(str(value or 0))
        if not amount.is_finite():
            raise ValueError
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BillingError("QuickBooks returned an invalid monetary amount.") from exc


def _date(value):
    return date.fromisoformat(value) if value else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _draft(session, draft_id):
    row = session.get(models.QBODraft, draft_id)
    if row is None:
        raise BillingError("Billing draft not found.")
    return row


def _totals(lines):
    # Existing billing rounds tax once per visit/job, including its chemicals.
    bases = defaultdict(int)
    for line in lines:
        if line["taxable"]:
            bases[(line["source_key"], line["tax_rate_ppm"])] += line["amount_cents"]
    tax_total = sum(tax.tax_cents(base, rate) for (_, rate), base in bases.items())
    return tax_total, sum(line["amount_cents"] for line in lines) + tax_total


def _source_lines(session, customer, first, last):
    props = [p.id for p in customer.properties]
    if not props:
        return [], []
    reserved = set(session.scalars(select(models.QBOBillingSource.source_key)).all())
    try:
        markup = float(models.get_setting(session, "chem_markup_percent", "0"))
    except ValueError:
        markup = 0
    lines, sources = [], []

    def add(doc, key, description, amount, item_kind, tax_flag, service_date, **extra):
        if amount < 0:
            raise BillingError(f"{description}: negative charges need review before billing.")
        lines.append(
            dict(
                source_key=key,
                description=description,
                amount_cents=amount,
                item_kind=item_kind,
                taxable=tax.tax_enabled(session) and tax._flag(session, tax_flag),
                tax_rate_ppm=tax.doc_tax_rate_ppm(session, doc),
                service_date=service_date.isoformat(),
                property_id=doc.property_id,
                **extra,
            )
        )

    visits = session.scalars(
        select(models.Visit)
        .where(
            models.Visit.property_id.in_(props),
            models.Visit.status == "done",
            models.Visit.date >= first,
            models.Visit.date <= last,
            models.Visit.paid_cash.is_(False),
        )
        .order_by(models.Visit.date, models.Visit.id)
    ).all()
    for visit in visits:
        key = f"visit:{visit.id}"
        if key in reserved:
            continue
        if visit.rate_cents is None and visit.labor_override_cents is None:
            raise BillingError(f"Visit {visit.id} has no frozen service price; price it before billing.")
        sources.append(key)
        add(
            visit,
            key,
            f"{visit.date}: {visit.property.label} service",
            tax.effective_labor_cents(visit),
            "service",
            "tax_weekly",
            visit.date,
        )
        for vc in visit.chemicals:
            add(
                visit,
                key,
                f"{visit.date}: {vc.chemical.name} ({vc.amount:g} {vc.chemical.unit})",
                qbo.billable_for_vc(vc, markup),
                "chemical",
                "tax_chems",
                visit.date,
                quantity=vc.amount,
                unit=vc.chemical.unit,
                visit_chemical_id=vc.id,
            )
    jobs = session.scalars(
        select(models.Job)
        .where(
            models.Job.property_id.in_(props),
            models.Job.status == "done",
            models.Job.paid_cash.is_(False),
            models.Job.completed_at >= datetime.combine(first - timedelta(days=1), time.min),
            models.Job.completed_at <= datetime.combine(last + timedelta(days=1), time.max),
        )
        .order_by(models.Job.completed_at, models.Job.id)
    ).all()
    for job in jobs:
        key = f"job:{job.id}"
        local_date = qbo._local_date(job.completed_at)
        if key in reserved or not first <= local_date <= last:
            continue
        sources.append(key)
        add(
            job,
            key,
            f"{local_date}: {job.title}",
            tax.effective_labor_cents(job),
            "service",
            "tax_labor",
            local_date,
            hours=job.hours,
        )
    return lines, sources


def prepare_month(session, year, month, user_id=None):
    scope = _scope(session)
    first, last = qbo._month_range(year, month)
    created = skipped = 0
    for customer in session.scalars(select(models.Customer).order_by(models.Customer.id)).all():
        if session.scalar(
            select(models.QBODraft.id).where(
                models.QBODraft.customer_id == customer.id,
                models.QBODraft.period_year == year,
                models.QBODraft.period_month == month,
            )
        ) or session.scalar(
            select(models.Invoice.id).where(
                models.Invoice.customer_id == customer.id,
                models.Invoice.period_year == year,
                models.Invoice.period_month == month,
                models.Invoice.status != "void",
            )
        ):
            skipped += 1
            continue
        lines, sources = _source_lines(session, customer, first, last)
        if not lines:
            continue
        tax_total, total = _totals(lines)
        draft = models.QBODraft(
            customer_id=customer.id,
            scope=scope,
            period_year=year,
            period_month=month,
            lines=lines,
            source_keys=sources,
            email=customer.email,
            due_date=last + timedelta(days=receivables.default_net_days(session)),
            tax_cents=tax_total,
            total_cents=total,
        )
        try:
            session.add(draft)
            session.flush()
            session.add_all(models.QBOBillingSource(source_key=key, draft_id=draft.id) for key in sources)
            models.audit(session, user_id, "qbo_billing.prepare", f"Draft {draft.id}; {total} cents")
            session.commit()
            created += 1
        except IntegrityError:
            session.rollback()
            skipped += 1
    return created, skipped


def edit_draft(session, draft_id, revision, lines, email, due_date, hold=False, user_id=None):
    row = _draft(session, draft_id)
    if len(lines) != len(row.lines):
        raise BillingError("Keep the source line items; adjust their amounts or hold the draft.")
    updated = []
    for old, incoming in zip(row.lines, lines):
        line = deepcopy(old)
        description = str(incoming.get("description", "")).strip()
        amount = incoming.get("amount_cents")
        if not description or len(description) > 4000 or type(amount) is not int or not 0 <= amount <= 100_000_000:
            raise BillingError("Each line needs a description and a valid nonnegative amount.")
        line.update(description=description, amount_cents=amount)
        updated.append(line)
    email = str(email).strip()
    if len(email) > 120 or "\n" in email or "\r" in email:
        raise BillingError("Invalid billing email.")
    if not isinstance(due_date, date):
        raise BillingError("A due date is required.")
    tax_total, total = _totals(updated)
    result = session.execute(
        update(models.QBODraft)
        .where(
            models.QBODraft.id == draft_id,
            models.QBODraft.revision == revision,
            models.QBODraft.status.in_(("draft", "held", "approved")),
            models.QBODraft.qbo_invoice_id == "",
        )
        .values(
            lines=updated,
            email=email,
            due_date=due_date,
            status="held" if hold else "draft",
            revision=revision + 1,
            tax_cents=tax_total,
            total_cents=total,
            approved_snapshot={},
            approved_at=None,
            approved_by_id=None,
            request_id="",
            last_error="",
            updated_at=models.utcnow(),
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise BillingError("This draft changed or was submitted. Reload before editing.")
    models.audit(session, user_id, "qbo_billing.edit", f"Draft {draft_id}; revision {revision + 1}")
    session.commit()
    session.refresh(row)
    return row


def _payload(session, row):
    scope = _scope(session)
    customer = row.customer
    if row.scope != scope or customer.qbo_customer_scope != scope or not customer.qbo_customer_id:
        raise BillingError("Sync this customer with the currently connected QuickBooks company first.")
    if models.get_setting(session, "qbo_item_scope", "") != scope:
        raise BillingError("Save service and chemical item mappings for this QuickBooks company first.")
    if "@" not in row.email or "\n" in row.email or "\r" in row.email:
        raise BillingError("A customer billing email is required.")
    if row.total_cents <= 0:
        raise BillingError("Only positive invoices can be approved.")
    lines = []
    for line in row.lines:
        key = "qbo_item_chem_id" if line["item_kind"] == "chemical" else "qbo_item_service_id"
        item = models.get_setting(session, key, "")
        if not item:
            raise BillingError("Map every service and chemical item before approval.")
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Description": line["description"],
                "Amount": line["amount_cents"] / 100,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": item},
                    "Qty": 1,
                    "UnitPrice": line["amount_cents"] / 100,
                    "ServiceDate": line["service_date"],
                    "TaxCodeRef": {"value": "TAX" if line["taxable"] else "NON"},
                },
            }
        )
    _, last = qbo._month_range(row.period_year, row.period_month)
    payload = {
        "CustomerRef": {"value": customer.qbo_customer_id},
        "Line": lines,
        "TxnDate": last.isoformat(),
        "DueDate": row.due_date.isoformat(),
        "BillEmail": {"Address": row.email},
        "EmailStatus": "NotSet",
        "AllowOnlineCreditCardPayment": True,
        "AllowOnlineACHPayment": True,
        "PrivateNote": f"OpenPoolPro billing draft {row.id}",
        "CurrencyRef": {"value": "USD"},
    }
    props = sorted(customer.properties, key=lambda p: p.id)
    # A customer can have multiple service locations; one ship-to would silently
    # tax all work at the first location. Require separate accounting review.
    taxable_props = {p.state.strip().upper() for p in props if p.state}
    if any(line["taxable"] for line in row.lines) and len(taxable_props) > 1:
        raise BillingError("Taxable work across multiple states needs separate billing review.")
    if props:
        prop = props[0]
        address = {
            k: v
            for k, v in {
                "Line1": prop.street or prop.address,
                "City": prop.city,
                "CountrySubDivisionCode": prop.state,
                "PostalCode": prop.zip,
                "Country": "US",
            }.items()
            if v
        }
        payload["BillAddr"] = address
        payload["ShipAddr"] = address
    return payload


def approve_draft(session, draft_id, revision, user):
    _enabled(session)
    if not user or not user.active or not user.can("approve_billing"):
        raise BillingError("Billing approval permission is required.")
    row = _draft(session, draft_id)
    _validate_sources(session, row)
    payload = _payload(session, row)
    snapshot = {
        "payload": payload,
        "lines": deepcopy(row.lines),
        "total_cents": row.total_cents,
        "tax_cents": row.tax_cents,
        "scope": row.scope,
        "revision": revision,
    }
    snapshot["digest"] = _digest(snapshot)
    result = session.execute(
        update(models.QBODraft)
        .where(models.QBODraft.id == draft_id, models.QBODraft.revision == revision, models.QBODraft.status == "draft")
        .values(
            status="approved",
            approved_snapshot=snapshot,
            approved_by_id=user.id,
            approved_at=models.utcnow(),
            request_id=str(uuid.uuid4()),
            updated_at=models.utcnow(),
            last_error="",
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise BillingError("This draft changed or is on hold. Reload before approving.")
    models.audit(session, user.id, "qbo_billing.approve", f"Draft {draft_id}; {row.total_cents} cents")
    session.commit()
    session.refresh(row)
    return row


def _snapshot(row):
    snapshot = deepcopy(row.approved_snapshot)
    digest = snapshot.pop("digest", "")
    if not digest or _digest(snapshot) != digest or snapshot.get("revision") != row.revision:
        raise BillingError("The approval snapshot is invalid; billing is stopped.")
    return snapshot


def _claim(session, row, expected, new):
    result = session.execute(
        update(models.QBODraft)
        .where(
            models.QBODraft.id == row.id,
            models.QBODraft.status.in_(expected),
            models.QBODraft.revision == row.revision,
            models.QBODraft.updated_at == row.updated_at,
        )
        .values(status=new, updated_at=models.utcnow(), last_error="")
    )
    session.commit()
    session.refresh(row)
    return result.rowcount == 1


def _mirror_invoice(session, scope, raw, customer_id=None):
    if "Id" not in raw or "TotalAmt" not in raw or "Balance" not in raw:
        raise BillingError("QuickBooks invoice is missing its identifier, total, or balance; sync stopped.")
    if raw.get("CurrencyRef", {}).get("value", "USD") != "USD":
        raise BillingError("A linked QuickBooks invoice uses a currency other than USD; sync needs accounting review.")
    qid = str(raw["Id"])
    mapped_customer_id = session.scalar(
        select(models.Customer.id).where(
            models.Customer.qbo_customer_scope == scope,
            models.Customer.qbo_customer_id == str(raw.get("CustomerRef", {}).get("value", "")),
        )
    )
    if mapped_customer_id is None or (customer_id is not None and mapped_customer_id != customer_id):
        return None
    customer_id = mapped_customer_id
    row = session.scalar(
        select(models.QBOInvoiceMirror).where(
            models.QBOInvoiceMirror.scope == scope, models.QBOInvoiceMirror.qbo_id == qid
        )
    )
    if row is None:
        row = models.QBOInvoiceMirror(scope=scope, qbo_id=qid, customer_id=customer_id)
        session.add(row)
    row.customer_id = customer_id
    row.number = str(raw.get("DocNumber", qid))
    row.total_cents = _cents(raw.get("TotalAmt"))
    row.balance_cents = _cents(raw.get("Balance"))
    row.due_date = _date(raw.get("DueDate"))
    row.txn_date = _date(raw.get("TxnDate"))
    row.email_status = raw.get("EmailStatus", "")
    link = raw.get("InvoiceLink") or raw.get("invoiceLink")
    if link:
        # Browser links are external input; prevent javascript/data schemes.
        from urllib.parse import urlparse

        parsed = urlparse(link)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and (parsed.hostname == "intuit.com" or parsed.hostname.endswith(".intuit.com"))
        ):
            row.payment_link = link
    row.status = "paid" if row.balance_cents == 0 else ("partial" if row.balance_cents < row.total_cents else "unpaid")
    if row.total_cents == 0 and str(raw.get("PrivateNote", "")).lower().startswith("void"):
        row.status = "void"
    row.raw = deepcopy(raw)
    row.last_synced_at = models.utcnow()
    session.flush()
    return row


def _matches(raw, snapshot):
    """Recheck both approved total and editable invoice content before delivery."""
    if _cents(raw.get("TotalAmt")) != snapshot["total_cents"]:
        return False
    if raw.get("CurrencyRef", {}).get("value", "USD") != "USD":
        return False
    expected = snapshot.get("reviewed_invoice")
    if expected:
        return _invoice_signature(raw) == expected
    payload = snapshot["payload"]
    if _cents(raw.get("TxnTaxDetail", {}).get("TotalTax")) != snapshot["tax_cents"]:
        return False
    if str(raw.get("CustomerRef", {}).get("value")) != payload["CustomerRef"]["value"]:
        return False
    if raw.get("TxnDate") != payload["TxnDate"]:
        return False
    if (
        raw.get("DueDate") != payload["DueDate"]
        or raw.get("BillEmail", {}).get("Address", "").lower() != payload["BillEmail"]["Address"].lower()
    ):
        return False
    actual = [line for line in raw.get("Line", []) if line.get("DetailType") == "SalesItemLineDetail"]
    wanted = payload["Line"]
    if len(actual) != len(wanted):
        return False
    for a, b in zip(actual, wanted):
        if (
            _cents(a.get("Amount")) != _cents(b["Amount"])
            or a.get("Description", "") != b["Description"]
            or str(a.get("SalesItemLineDetail", {}).get("ItemRef", {}).get("value"))
            != b["SalesItemLineDetail"]["ItemRef"]["value"]
        ):
            return False
        ad, bd = a.get("SalesItemLineDetail", {}), b["SalesItemLineDetail"]
        if (
            str(ad.get("TaxCodeRef", {}).get("value", "")) != bd["TaxCodeRef"]["value"]
            or Decimal(str(ad.get("Qty", 0))) != Decimal(str(bd["Qty"]))
            or _cents(ad.get("UnitPrice")) != _cents(bd["UnitPrice"])
        ):
            return False
    return True


def invoice_review_hash(raw):
    return _invoice_signature(raw)


def _invoice_signature(raw):
    return _digest(
        {
            key: raw.get(key)
            for key in (
                "SyncToken",
                "TotalAmt",
                "CustomerRef",
                "Line",
                "TxnTaxDetail",
                "DueDate",
                "BillEmail",
                "CurrencyRef",
            )
        }
    )


def submit_draft(session, draft_id):
    row = _draft(session, draft_id)
    with qbo_api.connection_guard(session, row.scope):
        return _submit_draft(session, draft_id)


def _submit_draft(session, draft_id):
    """Deliver only the approved immutable draft. Safe to resume after a crash."""
    _enabled(session)
    row = _draft(session, draft_id)
    if row.scope != _scope(session):
        raise BillingError("This draft belongs to a different QuickBooks connection.")
    snapshot = _snapshot(row)
    if row.status in ("sent", "delivery_unknown", "mismatch", "held", "draft"):
        return row
    _validate_sources(session, row)
    # An active sender is never stolen. A process crash requires reconciliation,
    # which observes EmailStatus; otherwise delivery remains explicitly unknown.
    if row.status == "sending":
        if row.updated_at < models.utcnow() - timedelta(minutes=10):
            row.status = "delivery_unknown"
            row.last_error = "Delivery was interrupted. Sync to check QuickBooks before taking further action."
            session.commit()
        return row
    if not row.qbo_invoice_id and row.status in ("create_unknown", "creating", "recovering"):
        if row.status in ("creating", "recovering") and row.updated_at > models.utcnow() - timedelta(minutes=10):
            return row
        if not _claim(session, row, ("create_unknown", "creating", "recovering"), "recovering"):
            return row
        candidates = [
            raw
            for raw in qbo_api.query_invoices(session)
            if raw.get("PrivateNote") == snapshot["payload"]["PrivateNote"]
        ]
        if (
            len(candidates) == 1
            and str(candidates[0].get("CustomerRef", {}).get("value")) == snapshot["payload"]["CustomerRef"]["value"]
        ):
            raw = candidates[0]
            row.qbo_invoice_id = str(raw["Id"])
            row.status = "ready" if _matches(raw, snapshot) else "mismatch"
            _mirror_invoice(session, row.scope, raw, row.customer_id)
            session.commit()
        else:
            row.status = "create_unknown"
            row.last_error = (
                "Creation could not be confirmed. Sync will search QuickBooks; "
                "no second invoice will be created. Verify the original request "
                "in QuickBooks if it remains missing."
            )
            session.commit()
            return row
    if not row.qbo_invoice_id:
        if row.status == "creating" and row.updated_at > models.utcnow() - timedelta(minutes=10):
            return row
        if not _claim(session, row, ("approved", "create_error", "create_retry", "creating"), "creating"):
            return row
        try:
            raw = qbo_api.create_invoice(session, snapshot["payload"], row.request_id)
        except qbo_api.QboError as exc:
            row.status = (
                "create_unknown" if exc.outcome_unknown else ("create_retry" if exc.retryable else "create_error")
            )
            row.last_error = str(exc)[:2000]
            session.commit()
            return row
        row.qbo_invoice_id = str(raw["Id"])
        row.status = "ready" if _matches(raw, snapshot) else "mismatch"
        if row.status == "mismatch":
            row.last_error = (
                "QuickBooks invoice differs from the approved draft. "
                "Review and approve its actual amounts before delivery."
            )
        _mirror_invoice(session, row.scope, raw, row.customer_id)
        session.commit()
    if row.status != "ready":
        return row
    # Read again immediately before sending so changes made directly in QBO
    # cannot slip through approval solely because the original create matched.
    raw = qbo_api.get_invoice(session, row.qbo_invoice_id)
    _mirror_invoice(session, row.scope, raw, row.customer_id)
    if not _matches(raw, snapshot):
        row.status = "mismatch"
        row.last_error = "The invoice changed in QuickBooks. Review it before delivery."
        session.commit()
        return row
    if raw.get("EmailStatus") == "EmailSent":
        row.status = "sent"
        session.commit()
        return row
    if not _claim(session, row, ("ready",), "sending"):
        return row
    try:
        sent = qbo_api.send_invoice(session, row.qbo_invoice_id, snapshot["payload"]["BillEmail"]["Address"])
    except qbo_api.QboError as exc:
        # Even HTTP failures may follow successful dispatch. Never resend from
        # a scheduler or a repeated click after an uncertain delivery.
        row.status = "delivery_unknown"
        row.last_error = f"Delivery needs verification in QuickBooks: {exc}"[:2000]
        session.commit()
        return row
    _mirror_invoice(session, row.scope, sent, row.customer_id)
    row.status = "sent"
    row.last_error = ""
    models.audit(
        session, row.approved_by_id, "qbo_billing.sent", f"Draft {row.id}; QuickBooks invoice {row.qbo_invoice_id}"
    )
    session.commit()
    return row


def approve_qbo_total(session, draft_id, revision, user, review_hash):
    row = _draft(session, draft_id)
    with qbo_api.connection_guard(session, row.scope):
        return _approve_qbo_total(session, draft_id, revision, user, review_hash)


def _approve_qbo_total(session, draft_id, revision, user, review_hash):
    """Approve the currently displayed mirror, never an unseen fresh total."""
    _enabled(session)
    if not user or not user.active or not user.can("approve_billing"):
        raise BillingError("Billing approval permission is required.")
    row = _draft(session, draft_id)
    _validate_sources(session, row)
    if row.scope != _scope(session) or row.status != "mismatch" or row.revision != revision:
        raise BillingError("Reload the invoice review before approving.")
    mirror = session.scalar(
        select(models.QBOInvoiceMirror).where(
            models.QBOInvoiceMirror.scope == row.scope, models.QBOInvoiceMirror.qbo_id == row.qbo_invoice_id
        )
    )
    if mirror is None:
        raise BillingError("Sync the invoice before reviewing its actual amounts.")
    if review_hash != _invoice_signature(mirror.raw):
        raise BillingError("The invoice changed since it was displayed. Reload before approving.")
    raw = qbo_api.get_invoice(session, row.qbo_invoice_id)
    if _invoice_signature(raw) != review_hash:
        _mirror_invoice(session, row.scope, raw, row.customer_id)
        session.commit()
        raise BillingError("QuickBooks changed since the review. Reload and review the new invoice.")
    if str(raw.get("CustomerRef", {}).get("value")) != row.approved_snapshot["payload"]["CustomerRef"]["value"]:
        raise BillingError("QuickBooks customer changed; correct the invoice in QuickBooks first.")
    if (
        raw.get("BillEmail", {}).get("Address", "").lower()
        != row.approved_snapshot["payload"]["BillEmail"]["Address"].lower()
    ):
        raise BillingError("QuickBooks billing email changed; correct it before approval.")
    if raw.get("CurrencyRef", {}).get("value", "USD") != "USD":
        raise BillingError("Only USD invoices can be approved for this billing workflow.")
    snapshot = _snapshot(row)
    snapshot.update(
        total_cents=_cents(raw.get("TotalAmt")),
        tax_cents=_cents(raw.get("TxnTaxDetail", {}).get("TotalTax")),
        reviewed_invoice=_invoice_signature(raw),
    )
    if snapshot["total_cents"] <= 0:
        raise BillingError("A zero or void invoice cannot be sent for payment.")
    snapshot["digest"] = _digest(snapshot)
    result = session.execute(
        update(models.QBODraft)
        .where(models.QBODraft.id == row.id, models.QBODraft.revision == revision, models.QBODraft.status == "mismatch")
        .values(
            status="ready",
            approved_snapshot=snapshot,
            approved_by_id=user.id,
            approved_at=models.utcnow(),
            total_cents=snapshot["total_cents"],
            tax_cents=snapshot["tax_cents"],
            updated_at=models.utcnow(),
            last_error="",
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise BillingError("This invoice was already reviewed. Reload.")
    models.audit(session, user.id, "qbo_billing.approve_actual", f"Draft {row.id}; {row.total_cents} cents")
    session.commit()
    return row


def process_pending(session):
    if models.get_setting(session, "qbo_billing_enabled", "0") != "1":
        return {"processed": 0}
    scope = _scope(session)
    rows = session.scalars(
        select(models.QBODraft).where(
            models.QBODraft.scope == scope,
            models.QBODraft.status.in_(
                ("approved", "creating", "create_unknown", "create_retry", "recovering", "ready", "sending")
            ),
        )
    ).all()
    count = 0
    for row in rows:
        try:
            submit_draft(session, row.id)
            count += 1
        except (BillingError, qbo_api.QboError) as exc:
            session.rollback()
            row = _draft(session, row.id)
            row.last_error = str(exc)[:2000]
            session.commit()
    return {"processed": count}


def reconcile(session):
    with qbo_api.connection_guard(session, _scope(session)):
        return _reconcile(session)


def _reconcile(session):
    """Full scoped snapshot, including external invoices and payment reversals.

    Fetch every page before changing balances or removing absent transactions.
    Invoice Balance is authoritative. Payment/credit rows retain their actual
    type and allocation, never inferred as cash from total minus balance.
    """
    scope = _scope(session)
    invoices = qbo_api.query_invoices(session)
    payments = qbo_api.query_payments(session)
    credits = qbo_api.query_credit_memos(session)
    refunds = qbo_api.query_refund_receipts(session)
    if scope != _scope(session):
        raise BillingError("QuickBooks connection changed during sync. Retry.")
    customers = {
        c.qbo_customer_id: c.id
        for c in session.scalars(
            select(models.Customer).where(
                models.Customer.qbo_customer_scope == scope, models.Customer.qbo_customer_id != ""
            )
        ).all()
    }
    seen_invoices = set()
    mirrors = {}
    for raw in invoices:
        customer_id = customers.get(str(raw.get("CustomerRef", {}).get("value", "")))
        if customer_id is None:
            continue
        mirror = _mirror_invoice(session, scope, raw, customer_id)
        mirrors[mirror.qbo_id] = mirror
        seen_invoices.add(mirror.qbo_id)
        mirror.allocation_known = True
        mirror.paid_cents = 0
        mirror.credit_cents = 0
    for old in session.scalars(select(models.QBOInvoiceMirror).where(models.QBOInvoiceMirror.scope == scope)).all():
        if old.qbo_id not in seen_invoices:
            # A previously seen invoice absent from a successful full query was
            # deleted or is no longer available; retain its audit record locally.
            old.status = "unavailable"
            old.email_status = ""
            old.payment_link = ""
            old.last_synced_at = models.utcnow()
    session.execute(delete(models.QBOTransactionMirror).where(models.QBOTransactionMirror.scope == scope))
    for kind, records in (("Payment", payments), ("CreditMemo", credits), ("RefundReceipt", refunds)):
        for raw in records:
            customer_id = customers.get(str(raw.get("CustomerRef", {}).get("value", "")))
            if customer_id is None:
                continue
            session.add(
                models.QBOTransactionMirror(
                    scope=scope,
                    kind=kind,
                    qbo_id=str(raw["Id"]),
                    customer_id=customer_id,
                    total_cents=_cents(raw.get("TotalAmt")),
                    txn_date=_date(raw.get("TxnDate")),
                    raw=_transaction_data(raw),
                )
            )
            if kind != "Payment":
                continue
            # Payment can link both Invoice and CreditMemo. A zero-total payment
            # applying a credit settles debt but contains no received cash.
            cash_left = max(0, _cents(raw.get("TotalAmt")))
            credit_links = any(
                link.get("TxnType") == "CreditMemo"
                for line in raw.get("Line", [])
                for link in line.get("LinkedTxn", [])
            )
            invoice_ids = {
                str(link.get("TxnId", ""))
                for line in raw.get("Line", [])
                for link in line.get("LinkedTxn", [])
                if link.get("TxnType") == "Invoice"
            }
            ambiguous = (credit_links and len(invoice_ids) > 1) or any(
                sum(link.get("TxnType") == "Invoice" for link in line.get("LinkedTxn", [])) > 1
                for line in raw.get("Line", [])
            )
            if ambiguous:
                for qid in invoice_ids:
                    if qid in mirrors:
                        mirrors[qid].allocation_known = False
                continue
            for line in raw.get("Line", []):
                invoice_links = [link for link in line.get("LinkedTxn", []) if link.get("TxnType") == "Invoice"]
                if len(invoice_links) != 1:
                    continue  # Preserve raw allocation rather than guess a split.
                mirror = mirrors.get(str(invoice_links[0].get("TxnId", "")))
                if mirror is None or mirror.customer_id != customer_id:
                    continue
                amount = max(0, _cents(line.get("Amount")))
                cash = min(amount, cash_left)
                mirror.paid_cents += cash
                cash_left -= cash
                if credit_links:
                    mirror.credit_cents += amount - cash
    for draft in session.scalars(
        select(models.QBODraft).where(
            models.QBODraft.scope == scope, models.QBODraft.status.in_(("sending", "delivery_unknown", "ready"))
        )
    ).all():
        mirror = mirrors.get(draft.qbo_invoice_id)
        if mirror and mirror.email_status == "EmailSent":
            draft.status = "sent"
            draft.last_error = ""
    models.set_setting(session, "qbo_billing_last_sync", models.utcnow().isoformat())
    models.set_setting(session, "qbo_billing_sync_scope", scope)
    models.set_setting(session, "qbo_billing_sync_error", "")
    session.commit()
    return {"invoices": len(mirrors), "payments": len(payments), "credits": len(credits), "refunds": len(refunds)}


def _transaction_data(raw):
    # Retain accounting allocations, not payment instrument or processor data.
    allowed = (
        "Id",
        "SyncToken",
        "TxnDate",
        "TotalAmt",
        "UnappliedAmt",
        "CustomerRef",
        "Line",
        "PaymentMethodRef",
        "PaymentRefNum",
        "DocNumber",
        "CurrencyRef",
        "MetaData",
    )
    return {key: deepcopy(raw[key]) for key in allowed if key in raw}


def discard_draft(session, draft_id, revision, user_id=None):
    row = _draft(session, draft_id)
    result = session.execute(
        update(models.QBODraft)
        .where(
            models.QBODraft.id == draft_id,
            models.QBODraft.revision == revision,
            models.QBODraft.status.in_(("draft", "held", "approved")),
            models.QBODraft.qbo_invoice_id == "",
        )
        .values(status="discarding")
    )
    if result.rowcount != 1:
        session.rollback()
        raise BillingError("Only an unsubmitted draft can be discarded.")
    session.execute(delete(models.QBOBillingSource).where(models.QBOBillingSource.draft_id == draft_id))
    session.delete(row)
    models.audit(session, user_id, "qbo_billing.discard", f"Draft {draft_id}; source reservations released")
    session.commit()

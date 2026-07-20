import io
import os
import zipfile
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, invoicing, models, qbo
from app.timefmt import to_local_date
from tests.test_analytics import _fix

YM = "2026-07"


def _generate(client, ym=YM):
    return client.post("/invoices/generate", data={"ym": ym}, follow_redirects=False)


def test_generate_creates_rows_and_files_matching_rollup(client, engine):
    _fix(engine)
    r = _generate(client)
    assert r.status_code == 303
    with Session(engine) as s:
        invs = s.scalars(select(models.Invoice)).all()
        rollup = {row.customer.name: row.total_cents for row in qbo.month_rollup(s, 2026, 7)}
        assert len(invs) == len(rollup) == 2
        for inv in invs:
            assert inv.total_cents == rollup[inv.customer.name]
            assert inv.status == "draft"
            assert inv.pdf_filename == f"{inv.number}.pdf"
            path = os.path.join(config.DATA_DIR, "invoices", inv.pdf_filename)
            assert os.path.isfile(path)
        # Atomic tmp+replace write must never leave a stray .tmp behind.
        invoices_dir = os.path.join(config.DATA_DIR, "invoices")
        assert not any(fn.endswith(".tmp") for fn in os.listdir(invoices_dir))


def test_generate_is_idempotent(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        before = {i.number for i in s.scalars(select(models.Invoice)).all()}

    created, skipped = None, None
    with Session(engine) as s:
        created, skipped = invoicing.generate_month(s, 2026, 7, 1)
    assert (created, skipped) == (0, 2)
    with Session(engine) as s:
        after = {i.number for i in s.scalars(select(models.Invoice)).all()}
    assert before == after


def test_generate_on_empty_month_creates_nothing_without_error(client, engine):
    r = _generate(client, ym="2030-01")
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.scalars(select(models.Invoice)).all() == []
        created, skipped = invoicing.generate_month(s, 2030, 1, 1)
    assert (created, skipped) == (0, 0)


def test_void_then_regenerate_month_gives_new_number(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        alpha_inv = s.scalars(select(models.Invoice).join(models.Customer).where(
            models.Customer.name == "Alpha")).one()
        old_number = alpha_inv.number
        alpha_id = alpha_inv.id

    client.post(f"/invoices/{alpha_id}/status", data={"action": "void"})
    with Session(engine) as s:
        created, skipped = invoicing.generate_month(s, 2026, 7, 1)
    assert created == 1 and skipped == 1  # Alpha re-created (void), Beta skipped

    with Session(engine) as s:
        alpha_invoices = s.scalars(select(models.Invoice).join(models.Customer).where(
            models.Customer.name == "Alpha")).all()
        numbers = {i.number for i in alpha_invoices}
        assert old_number in numbers
        assert len(numbers) == 2
        new_number = (numbers - {old_number}).pop()
        assert new_number != old_number


def test_status_transitions_and_void_terminal(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid = inv.id

    # draft -> sent OK
    r = client.post(f"/invoices/{iid}/status", data={"action": "sent"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).status == "sent"

    # sent -> sent is not a valid transition
    r = client.post(f"/invoices/{iid}/status", data={"action": "sent"}, follow_redirects=False)
    assert r.status_code == 400

    # sent -> void OK
    r = client.post(f"/invoices/{iid}/status", data={"action": "void"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).status == "void"

    # void is terminal: sent and void actions both 400
    r = client.post(f"/invoices/{iid}/status", data={"action": "sent"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.post(f"/invoices/{iid}/status", data={"action": "void"}, follow_redirects=False)
    assert r.status_code == 400

    # regenerate on void -> 400
    r = client.post(f"/invoices/{iid}/regenerate", follow_redirects=False)
    assert r.status_code == 400


def test_draft_to_void_direct_transition_ok(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid = inv.id
    r = client.post(f"/invoices/{iid}/status", data={"action": "void"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(models.Invoice, iid).status == "void"


def test_regenerate_rerenders_same_number_and_updates_total(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice).join(models.Customer).where(
            models.Customer.name == "Alpha")).one()
        iid, number, old_generated, old_total = inv.id, inv.number, inv.generated_at, inv.total_cents
        customer_id = inv.customer_id
        property_id = inv.customer.properties[0].id
        path = os.path.join(config.DATA_DIR, "invoices", inv.pdf_filename)
        before_bytes = open(path, "rb").read()

    # Mutate source data: add another done visit with a rate in the same
    # billing period, so the regenerated total must actually change.
    with Session(engine) as s:
        s.add(models.Visit(property_id=property_id, date=date(2026, 7, 20),
                           status="done", rate_cents=9999))
        s.commit()
        expected_total = invoicing.invoice_data(
            s, s.get(models.Customer, customer_id), 2026, 7).total_cents
    assert expected_total == old_total + 9999

    r = client.post(f"/invoices/{iid}/regenerate", follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        inv = s.get(models.Invoice, iid)
        assert inv.number == number  # same number, not a new invoice
        assert inv.generated_at >= old_generated
        assert inv.total_cents == expected_total
        path = os.path.join(config.DATA_DIR, "invoices", inv.pdf_filename)
        assert os.path.isfile(path)
        after_bytes = open(path, "rb").read()
    assert after_bytes != before_bytes
    # Atomic tmp+replace write must never leave a stray .tmp behind.
    invoices_dir = os.path.join(config.DATA_DIR, "invoices")
    assert not any(fn.endswith(".tmp") for fn in os.listdir(invoices_dir))


def test_pdf_serve_200_and_404_missing(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid = inv.id
        path = os.path.join(config.DATA_DIR, "invoices", inv.pdf_filename)

    r = client.get(f"/invoices/{iid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"

    os.remove(path)
    r = client.get(f"/invoices/{iid}/pdf")
    assert r.status_code == 404

    r = client.get("/invoices/99999/pdf")
    assert r.status_code == 404


def test_batch_zip_lists_exactly_non_void_filenames(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        void_inv = s.scalars(select(models.Invoice).join(models.Customer).where(
            models.Customer.name == "Alpha")).one()
        void_id = void_inv.id
        all_invs = s.scalars(select(models.Invoice)).all()
        expected_non_void = {i.pdf_filename for i in all_invs if i.id != void_id}

    client.post(f"/invoices/{void_id}/status", data={"action": "void"})

    r = client.get(f"/invoices/batch.zip?ym={YM}")
    assert r.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == expected_non_void


def test_register_filters_by_status(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid, number = inv.id, inv.number
    client.post(f"/invoices/{iid}/status", data={"action": "sent"})

    r = client.get(f"/invoices?ym={YM}&status=sent")
    assert r.status_code == 200
    assert number in r.text

    r = client.get(f"/invoices?ym={YM}&status=draft")
    assert r.status_code == 200
    assert number not in r.text

    r = client.get(f"/invoices?ym={YM}")
    assert r.status_code == 200
    assert number in r.text  # unfiltered shows all


def test_register_shows_pdf_missing_tag_after_file_deleted(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        path = os.path.join(config.DATA_DIR, "invoices", inv.pdf_filename)

    r = client.get(f"/invoices?ym={YM}")
    assert r.status_code == 200
    assert "pdf missing" not in r.text

    os.remove(path)
    r = client.get(f"/invoices?ym={YM}")
    assert r.status_code == 200
    assert "pdf missing" in r.text


def test_register_shows_generated_column(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        expected = to_local_date(inv.generated_at)

    r = client.get(f"/invoices?ym={YM}")
    assert r.status_code == 200
    assert "Generated" in r.text  # column header
    assert expected in r.text


def test_generate_month_skips_row_on_integrity_error(client, engine, monkeypatch):
    """Simulates a concurrent-generate race: two callers can compute the
    same next_number() before either commits, colliding on the Invoice.number
    unique constraint. generate_month must catch that per-row IntegrityError,
    roll back, and count it as skipped rather than crashing the whole batch."""
    _fix(engine)
    with Session(engine) as s:
        other = models.Customer(name="Zulu")
        s.add(other)
        s.commit()
        # Pre-seed the number generate_month would allocate for Alpha/Beta,
        # under an unrelated customer/period so the existing-invoice check
        # doesn't skip Alpha/Beta outright — the collision must surface as
        # an IntegrityError on the real insert instead.
        s.add(models.Invoice(number="INV-2026-0001", customer_id=other.id,
                              period_year=1999, period_month=1, total_cents=0,
                              status="draft", pdf_filename="x.pdf"))
        s.commit()

    monkeypatch.setattr(invoicing, "next_number", lambda session, year: "INV-2026-0001")

    with Session(engine) as s:
        created, skipped = invoicing.generate_month(s, 2026, 7, 1)
    assert (created, skipped) == (0, 2)
    with Session(engine) as s:
        # Only the pre-seeded row remains; failed rows left nothing behind.
        assert len(s.scalars(select(models.Invoice)).all()) == 1


def test_register_tolerant_bad_ym_and_status(client, engine):
    _fix(engine)
    r = client.get("/invoices?ym=garbage&status=bogus")
    assert r.status_code == 200


def test_tech_403_sweep(client, engine, tech_client):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid = inv.id
    t = tech_client(name="notadmin")
    assert t.get(f"/invoices?ym={YM}", follow_redirects=False).status_code == 403
    assert t.post("/invoices/generate", data={"ym": YM}, follow_redirects=False).status_code == 403
    assert t.get(f"/invoices/{iid}/pdf", follow_redirects=False).status_code == 403
    assert t.post(f"/invoices/{iid}/status", data={"action": "sent"},
                   follow_redirects=False).status_code == 403
    assert t.post(f"/invoices/{iid}/regenerate", follow_redirects=False).status_code == 403
    assert t.get(f"/invoices/batch.zip?ym={YM}", follow_redirects=False).status_code == 403


def test_nav_item_admin_gated(client, tech_client):
    t = tech_client(name="baretech")
    tech_html = t.get("/customers").text
    assert 'href="/invoices"' not in tech_html
    admin_html = client.get("/customers").text
    assert 'href="/invoices"' in admin_html


# ---------------------------------------------------------------------------
# Audit wiring
# ---------------------------------------------------------------------------

def test_generate_audits_invoice_generated_with_created_count(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "invoice.generated")).all()
        assert len(rows) == 1
        assert rows[0].detail == f"{YM} created=2"


def test_generate_on_empty_month_still_audits_zero_created(client, engine):
    r = _generate(client, ym="2030-01")
    assert r.status_code == 303
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "invoice.generated")).all()
        assert len(rows) == 1
        assert rows[0].detail == "2030-01 created=0"


def test_status_change_audits_invoice_status(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid, number = inv.id, inv.number

    client.post(f"/invoices/{iid}/status", data={"action": "sent"})
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "invoice.status")).all()
        assert len(rows) == 1
        assert rows[0].detail == f"{number} -> sent"


def test_regenerate_audits_as_invoice_generated(client, engine):
    _fix(engine)
    _generate(client)
    with Session(engine) as s:
        inv = s.scalars(select(models.Invoice)).first()
        iid, number = inv.id, inv.number

    client.post(f"/invoices/{iid}/regenerate")
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(
            models.AuditLog.action == "invoice.generated").order_by(models.AuditLog.id)).all()
        # One from the original generate call, one from regenerate.
        assert len(rows) == 2
        assert rows[-1].detail == f"regenerate {number}"

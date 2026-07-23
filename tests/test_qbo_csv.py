from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.qbo import export_csv
from tests.test_money import seed_month


def test_csv_golden(engine):
    from app.db import Base
    from app import seeds
    Base.metadata.create_all(engine)
    seeds.run(engine)
    seed_month(engine)
    with Session(engine) as s:
        got = export_csv(s, 2026, 7)
    expected = (
        "Customer,Service Date,Product/Service,Description,Amount\r\n"
        "Bill Me LLC,2026-07-31,Pool Service — Labor,2 visits Jul 2026,110.00\r\n"
        "Bill Me LLC,2026-07-31,Pool Service — Chemicals,Chemicals Jul 2026,9.00\r\n"
        "Bill Me LLC,2026-07-20,Pool Service — Job,Filter clean,125.00\r\n"
    )
    assert got == expected


def test_export_route_downloads(client, engine):
    seed_month(engine)
    r = client.get("/money/export?ym=2026-07")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "Bill Me LLC" in r.text


def test_csv_golden_with_tax(engine):
    from app.db import Base
    from app import seeds
    Base.metadata.create_all(engine)
    seeds.run(engine)
    seed_month(engine)
    with Session(engine) as s:
        models.set_setting(s, "sales_tax_enabled", "1")
        seeds.seed_tax_rates(s)
        p = s.scalars(select(models.Property)).one()
        p.state = "NJ"
        s.commit()
        got = export_csv(s, 2026, 7)
    lines = got.splitlines()
    tax_lines = [ln for ln in lines if "Pool Service — Sales Tax" in ln]
    # v1 (labor 5500 + chems 900) @ 6.625% = 424; v2 (labor 5500) = 364;
    # job (labor 12500) = 828 -> summed 1616 cents = $16.16
    assert tax_lines == ["Bill Me LLC,2026-07-31,Pool Service — Sales Tax,Sales tax Jul 2026,16.16"]


def test_export_route_writes_export_qbo_audit_row(client, engine):
    seed_month(engine)
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.get("/money/export?ym=2026-07")
    with Session(engine) as s:
        rows = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "export.qbo")).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id
        assert "2026-07" in rows[0].detail

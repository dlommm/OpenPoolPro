from sqlalchemy.orm import Session

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

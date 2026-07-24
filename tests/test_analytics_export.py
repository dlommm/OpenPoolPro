"""Charts module + Excel/PDF export routes."""
from app import charts
from tests.test_analytics import RANGE, _fix

TABS = ["income", "customers", "crew", "types", "chemicals", "volume", "rates"]


# --- charts module ---------------------------------------------------------
def test_bar_chart_renders_svg_with_escaped_label():
    svg = charts.bar_chart([("W<27>", 12000), ("W28", 8000)], kind="money")
    assert svg.startswith("<svg") and "</svg>" in svg
    assert "&lt;27&gt;" in svg and "<27>" not in svg   # label escaped, no injection


def test_hbar_and_donut_and_line_render():
    assert "<svg" in charts.hbar_chart([("Alice", 5000), ("Bob", 3000)])
    assert "<svg" in charts.donut_chart([("Visits", 5000), ("Repairs", 3000)])
    assert "<svg" in charts.line_chart([("W1", 100), ("W2", 200), ("W3", 150)])


def test_empty_series_render_placeholder_not_crash():
    assert "No data" in charts.bar_chart([], empty="No data")
    assert "No data" in charts.hbar_chart([("x", 0)], empty="No data")


# --- page shows KPIs + chart ----------------------------------------------
def test_analytics_page_has_kpis_and_chart(client, engine):
    _fix(engine)
    r = client.get("/analytics", params={"tab": "income",
                   "start": RANGE[0].isoformat(), "end": RANGE[1].isoformat()})
    assert r.status_code == 200
    assert "Revenue" in r.text and "Top customer" in r.text   # KPI tiles
    assert "<svg" in r.text                                    # inline chart


# --- Excel export ----------------------------------------------------------
def test_export_xlsx_all_tabs(client, engine):
    _fix(engine)
    for tab in TABS:
        r = client.get("/analytics/export.xlsx", params={"tab": tab,
                       "start": RANGE[0].isoformat(), "end": RANGE[1].isoformat()})
        assert r.status_code == 200, tab
        assert "spreadsheetml" in r.headers["content-type"], tab
        assert r.content[:2] == b"PK", tab                     # xlsx = zip
        assert ".xlsx" in r.headers["content-disposition"]


# --- PDF export ------------------------------------------------------------
def test_export_pdf_all_tabs(client, engine):
    _fix(engine)
    for tab in TABS:
        r = client.get("/analytics/export.pdf", params={"tab": tab,
                       "start": RANGE[0].isoformat(), "end": RANGE[1].isoformat()})
        assert r.status_code == 200, tab
        assert r.headers["content-type"] == "application/pdf", tab
        assert r.content[:5] == b"%PDF-", tab


def test_export_unknown_tab_404(client):
    assert client.get("/analytics/export.xlsx", params={"tab": "nope"}).status_code == 404
    assert client.get("/analytics/export.pdf", params={"tab": "nope"}).status_code == 404


def test_export_requires_admin(tech_client, engine):
    _fix(engine)
    c = tech_client("sam")
    assert c.get("/analytics/export.xlsx", params={"tab": "income"}).status_code == 403
    assert c.get("/analytics/export.pdf", params={"tab": "income"}).status_code == 403

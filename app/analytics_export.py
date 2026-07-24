"""Excel (.xlsx) and PDF exports for the analytics tabs, each with a chart.

One source of truth for a tab's columns and its chart series (`table_for`), so
the CSV, Excel and PDF exports and the on-screen chart all agree. Money is stored
as integer cents everywhere and only divided to dollars at the render edge here.
"""
import io


def _d(cents) -> float:
    return round((cents or 0) / 100, 2)


def table_for(tab: str, rows):
    """Return (headers, data_rows, chart) for a tab.

    headers: list[str]; data_rows: list[list] (money already in dollar floats);
    chart: dict(kind='bar'|'pie'|None, title, labels, values, value_kind) — the
    values are the raw plotted numbers (dollars for money, ints for counts).
    """
    if tab == "income":
        headers = ["Week start", "ISO week", "Labor", "Chem cost", "Chem billable",
                   "Jobs", "Revenue", "Profit"]
        data = [[str(r.week_start_date), r.iso_week, _d(r.labor_cents), _d(r.chem_cost_cents),
                 _d(r.chem_billable_cents), _d(r.job_cents), _d(r.total_cents), _d(r.profit_cents)]
                for r in rows]
        chart = {"kind": "bar", "title": "Revenue by week", "value_kind": "money",
                 "labels": [f"W{r.iso_week}" for r in rows],
                 "values": [_d(r.total_cents) for r in rows]}
    elif tab == "customers":
        headers = ["Customer", "Visits", "Labor", "Chemicals", "Jobs", "Total"]
        data = [[r.customer.name, r.visit_count, _d(r.labor_cents), _d(r.chem_billable_cents),
                 _d(r.job_cents), _d(r.total_cents)] for r in rows]
        top = rows[:10]
        chart = {"kind": "bar", "title": "Revenue by customer", "value_kind": "money",
                 "labels": [r.customer.name for r in top], "values": [_d(r.total_cents) for r in top]}
    elif tab == "crew":
        headers = ["Tech", "Visits", "Jobs", "Minutes", "Revenue", "Rev / hr"]
        data = [[(r.user.name if r.user else "Unattributed"), r.visit_count, r.job_count,
                 r.total_minutes, _d(r.total_cents),
                 _d(r.revenue_per_hour_cents) if r.revenue_per_hour_cents is not None else ""]
                for r in rows]
        chart = {"kind": "bar", "title": "Revenue by tech", "value_kind": "money",
                 "labels": [(r.user.name if r.user else "Unattributed") for r in rows],
                 "values": [_d(r.total_cents) for r in rows]}
    elif tab == "types":
        headers = ["Type", "Total"]
        data = [[r.label, _d(r.total_cents)] for r in rows]
        chart = {"kind": "pie", "title": "Job-type mix", "value_kind": "money",
                 "labels": [r.label for r in rows], "values": [_d(r.total_cents) for r in rows]}
    elif tab == "chemicals":
        headers = ["Chemical", "Quantity", "Unit", "Cost", "Billable", "Margin"]
        data = [[r.chemical.name, round(r.quantity, 2), r.chemical.unit, _d(r.cost_cents),
                 _d(r.billable_cents), _d(r.margin_cents)] for r in rows]
        ranked = sorted(rows, key=lambda r: r.margin_cents, reverse=True)[:10]
        chart = {"kind": "bar", "title": "Margin by chemical", "value_kind": "money",
                 "labels": [r.chemical.name for r in ranked], "values": [_d(r.margin_cents) for r in ranked]}
    elif tab == "volume":
        headers = ["Week start", "ISO week", "Done", "Skipped", "Avg minutes"]
        data = [[str(r.week_start_date), r.iso_week, r.done, r.skipped,
                 r.avg_minutes if r.avg_minutes is not None else ""] for r in rows]
        chart = {"kind": "bar", "title": "Visits done by week", "value_kind": "int",
                 "labels": [f"W{r.iso_week}" for r in rows], "values": [r.done for r in rows]}
    else:  # rates
        headers = ["Customer", "Property", "Current", "Tier", "Delta"]
        data = [[r.property.customer.name, r.property.label, _d(r.current_cents),
                 _d(r.tier_cents), _d(r.delta_cents)] for r in rows]
        chart = None
    return headers, data, chart


def _kpi_pairs(summary: dict) -> list[tuple[str, str]]:
    return [
        ("Revenue", f"${_d(summary['revenue_cents']):,.2f}"),
        ("Profit", f"${_d(summary['profit_cents']):,.2f}"),
        ("Visits done", str(summary["visits_done"])),
        ("Avg / visit", f"${_d(summary['avg_per_visit_cents']):,.2f}"),
        ("Customers", str(summary["customer_count"])),
        ("Top customer", (summary["top_customer"] or "—")),
    ]


# ----------------------------------------------------------------- Excel -----
def build_xlsx(tab_label: str, start, end, summary: dict, tab: str, rows) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, PieChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    headers, data, chart = table_for(tab, rows)
    money_cols = {i for i, h in enumerate(headers)
                  if h in ("Labor", "Chem cost", "Chem billable", "Jobs", "Revenue",
                           "Profit", "Total", "Chemicals", "Cost", "Billable", "Margin",
                           "Current", "Tier", "Delta", "Rev / hr")}

    wb = Workbook()
    ws = wb.active
    ws.title = tab_label[:31]
    accent = Font(bold=True, size=14, color="16788D")
    ws["A1"] = f"OpenPoolPro — {tab_label}"
    ws["A1"].font = accent
    ws["A2"] = f"Period: {start} to {end}"
    ws["A2"].font = Font(italic=True, color="808080")

    # KPI band
    r = 4
    for i, (k, v) in enumerate(_kpi_pairs(summary)):
        c = 1 + i * 2
        ws.cell(row=r, column=c, value=k).font = Font(bold=True, color="808080", size=9)
        ws.cell(row=r + 1, column=c, value=v).font = Font(bold=True, size=12)

    # Data table
    head_row = r + 3
    header_fill = PatternFill("solid", fgColor="16788D")
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(row=head_row, column=j, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left")
    for i, drow in enumerate(data):
        for j, val in enumerate(drow):
            cell = ws.cell(row=head_row + 1 + i, column=j + 1, value=val)
            if (j) in money_cols and isinstance(val, (int, float)):
                cell.number_format = '$#,##0.00'
    first_data = head_row + 1
    last_data = head_row + len(data)

    # Column widths
    for j, h in enumerate(headers, start=1):
        width = max(len(str(h)), *(len(str(row[j - 1])) for row in data)) if data else len(str(h))
        ws.column_dimensions[get_column_letter(j)].width = min(34, width + 3)

    # Native chart from the plotted column
    if chart and data and chart["values"] and any(v for v in chart["values"]):
        # locate the plotted value column + a label column
        val_header = {"income": "Revenue", "customers": "Total", "crew": "Revenue",
                      "types": "Total", "chemicals": "Margin", "volume": "Done"}.get(tab)
        label_header = {"income": "ISO week", "customers": "Customer", "crew": "Tech",
                        "types": "Type", "chemicals": "Chemical", "volume": "ISO week"}.get(tab)
        if val_header in headers and label_header in headers:
            vcol = headers.index(val_header) + 1
            lcol = headers.index(label_header) + 1
            anchor_col = get_column_letter(len(headers) + 2)
            if chart["kind"] == "pie":
                ch = PieChart()
            else:
                ch = BarChart()
                ch.type = "col"
                ch.y_axis.title = "Dollars" if chart["value_kind"] == "money" else "Count"
            ch.title = chart["title"]
            ch.height = 8
            ch.width = 16
            values = Reference(ws, min_col=vcol, min_row=head_row, max_row=last_data)
            cats = Reference(ws, min_col=lcol, min_row=first_data, max_row=last_data)
            ch.add_data(values, titles_from_data=True)
            ch.set_categories(cats)
            ch.legend = None if chart["kind"] == "bar" else ch.legend
            ws.add_chart(ch, f"{anchor_col}{head_row}")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------- PDF -----
def build_pdf(tab_label: str, start, end, summary: dict, tab: str, rows) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle)

    headers, data, chart = table_for(tab, rows)
    styles = getSampleStyleSheet()
    accent = colors.HexColor("#16788D")
    brass = colors.HexColor("#8f6114")
    title_style = ParagraphStyle("t", parent=styles["Title"], textColor=accent, fontSize=18)
    sub = ParagraphStyle("s", parent=styles["Normal"], textColor=colors.grey, fontSize=9)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch, leftMargin=0.6 * inch, rightMargin=0.6 * inch)
    story = [Paragraph(f"OpenPoolPro — {tab_label}", title_style),
             Paragraph(f"Period: {start} to {end}", sub), Spacer(1, 14)]

    # KPI table (2 columns of key/value)
    pairs = _kpi_pairs(summary)
    kpi_rows = [[k1, v1, k2, v2] for (k1, v1), (k2, v2) in zip(pairs[0::2], pairs[1::2])]
    kt = Table(kpi_rows, colWidths=[1.4 * inch, 1.7 * inch, 1.4 * inch, 1.7 * inch])
    kt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [kt, Spacer(1, 16)]

    # Chart
    if chart and chart["values"] and any(v for v in chart["values"]):
        story.append(_pdf_chart(chart, accent))
        story.append(Spacer(1, 16))

    # Data table
    tbl_data = [headers] + [[_pdf_cell(c, headers[j]) for j, c in enumerate(row)] for row in data]
    if not data:
        tbl_data.append(["No data for this range."] + [""] * (len(headers) - 1))
    t = Table(tbl_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dfe4ea")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


def _pdf_cell(value, header):
    money_headers = ("Labor", "Chem cost", "Chem billable", "Jobs", "Revenue", "Profit",
                     "Total", "Chemicals", "Cost", "Billable", "Margin", "Current", "Tier",
                     "Delta", "Rev / hr")
    if header in money_headers and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    return "" if value is None else str(value)


def _pdf_chart(chart, accent):
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.lib import colors

    labels = [str(x) for x in chart["labels"]][:10]
    values = [float(v) for v in chart["values"]][:10]
    d = Drawing(460, 200)
    d.add(String(0, 186, chart["title"], fontName="Helvetica-Bold", fontSize=11,
                 fillColor=colors.HexColor("#223333")))
    if chart["kind"] == "pie":
        pie = Pie()
        pie.x, pie.y, pie.width, pie.height = 150, 20, 150, 150
        pie.data = values
        pie.labels = labels
        palette = [colors.HexColor("#16788D"), colors.HexColor("#8f6114"),
                   colors.HexColor("#0f766e"), colors.HexColor("#b23a1e")]
        for i in range(len(values)):
            pie.slices[i].fillColor = palette[i % len(palette)]
        pie.sideLabels = True
        d.add(pie)
    else:
        bc = VerticalBarChart()
        bc.x, bc.y, bc.width, bc.height = 30, 25, 410, 150
        bc.data = [values]
        bc.categoryAxis.categoryNames = labels
        bc.categoryAxis.labels.angle = 30
        bc.categoryAxis.labels.dy = -6
        bc.categoryAxis.labels.fontSize = 7
        bc.valueAxis.valueMin = 0
        bc.bars[0].fillColor = accent
        bc.barWidth = 8
        d.add(bc)
    return d

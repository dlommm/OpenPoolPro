"""Server-rendered inline-SVG charts for the analytics screen.

Self-contained SVG (no client JS, no external libs) so it satisfies the app CSP
and renders inside the offline PWA. Marks are colored with the active theme's CSS
custom properties (var(--color-pool) etc.), so one chart reads correctly in every
theme and in light/dark without per-theme code. All axis/value text uses ink/mist
tokens — never the series color (dataviz rule). Any caller-supplied label is
passed through html.escape before it reaches the SVG (customer/tech names are
user data).

Money helpers take integer cents. Charts return an SVG string to embed with
`{{ chart | safe }}` — the markup is generated here, never from request input.
"""
import html

# A small fixed categorical order for composition charts (job types, ≤4 slices).
# Distinct hue families (teal / gold / green / rose) so they separate for most
# CVD types; every slice is also directly labelled, which is the secondary
# encoding the dataviz guidance requires when relying on ≤4 hues.
_CAT = ["var(--color-pool)", "var(--color-brass)", "var(--cr-green, #10b981)",
        "var(--color-safety)"]


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _money(cents) -> str:
    return f"${(cents or 0) / 100:,.0f}"


def _fmt(value, kind: str) -> str:
    if kind == "money":
        return _money(value)
    if kind == "int":
        return f"{int(value):,}"
    return str(value)


def bar_chart(items, *, kind="money", height=220, max_bars=12, empty="No data"):
    """Vertical bar chart for a short ordered series (income/volume by week).

    items: list of (label, value) with value a number (cents for money). Bars use
    the single theme hue (magnitude job). Values are labelled above each bar.
    """
    items = list(items)[:max_bars]
    if not items or all((v or 0) <= 0 for _, v in items):
        return _empty(empty, height)
    vals = [max(0, v or 0) for _, v in items]
    vmax = max(vals) or 1
    pad_l, pad_r, pad_t, pad_b = 8, 8, 22, 34
    w = 760
    plot_w = w - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(items)
    gap = 10
    bw = max(6, (plot_w - gap * (n - 1)) / n)
    parts = [f'<svg viewBox="0 0 {w} {height}" class="w-full" role="img" '
             f'preserveAspectRatio="xMidYMid meet" style="max-height:{height}px">']
    # baseline
    y0 = pad_t + plot_h
    parts.append(f'<line x1="{pad_l}" y1="{y0}" x2="{w - pad_r}" y2="{y0}" '
                 f'stroke="var(--color-seam)" stroke-width="1"/>')
    for i, (label, v) in enumerate(items):
        v = max(0, v or 0)
        x = pad_l + i * (bw + gap)
        bh = (v / vmax) * plot_h if vmax else 0
        y = y0 - bh
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'rx="4" fill="var(--color-pool)"/>')
        parts.append(
            f'<text x="{x + bw / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" '
            f'font-size="11" font-family="var(--font-data)" fill="var(--color-ink)">'
            f'{_esc(_fmt(v, kind))}</text>')
        parts.append(
            f'<text x="{x + bw / 2:.1f}" y="{y0 + 16:.1f}" text-anchor="middle" '
            f'font-size="10.5" fill="var(--color-mist)">{_esc(label)}</text>')
    parts.append('</svg>')
    return "".join(parts)


def hbar_chart(items, *, kind="money", row_h=30, max_bars=10, empty="No data"):
    """Horizontal bars for a ranking (revenue by customer / tech / chemical).

    items: list of (label, value), pre-sorted desc by the caller. Label sits on
    the bar's left, value at its end; single theme hue.
    """
    items = list(items)[:max_bars]
    if not items or all((v or 0) <= 0 for _, v in items):
        return _empty(empty, 120)
    vmax = max(max(0, v or 0) for _, v in items) or 1
    w = 760
    label_w = 190
    val_w = 78
    bar_area = w - label_w - val_w - 8
    height = len(items) * row_h + 8
    parts = [f'<svg viewBox="0 0 {w} {height}" class="w-full" role="img" '
             f'preserveAspectRatio="xMidYMid meet" style="max-height:{height}px">']
    for i, (label, v) in enumerate(items):
        v = max(0, v or 0)
        y = i * row_h + 4
        bh = row_h - 12
        bw = (v / vmax) * bar_area if vmax else 0
        parts.append(
            f'<text x="0" y="{y + bh / 2 + 4:.1f}" font-size="12" '
            f'fill="var(--color-ink)">{_esc(_truncate(label, 26))}</text>')
        parts.append(
            f'<rect x="{label_w}" y="{y:.1f}" width="{max(2, bw):.1f}" height="{bh}" '
            f'rx="4" fill="var(--color-pool)"/>')
        parts.append(
            f'<text x="{w}" y="{y + bh / 2 + 4:.1f}" text-anchor="end" font-size="12" '
            f'font-family="var(--font-data)" fill="var(--color-ink)">'
            f'{_esc(_fmt(v, kind))}</text>')
    parts.append('</svg>')
    return "".join(parts)


def donut_chart(slices, *, kind="money", size=220, empty="No data"):
    """Donut for composition (job-type / chemical mix). slices: list of
    (label, value); ≤4 recommended. Legend + value are rendered by the template
    beside the ring, so identity is never color-alone."""
    slices = [(l, max(0, v or 0)) for l, v in slices if (v or 0) > 0]
    total = sum(v for _, v in slices)
    if not total:
        return _empty(empty, size)
    import math
    cx = cy = size / 2
    r = size / 2 - 6
    inner = r * 0.58
    parts = [f'<svg viewBox="0 0 {size} {size}" role="img" '
             f'style="width:{size}px;height:{size}px;max-width:100%">']
    a0 = -math.pi / 2  # start at top
    for i, (_label, v) in enumerate(slices):
        frac = v / total
        a1 = a0 + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x0o, y0o = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1o, y1o = cx + r * math.cos(a1), cy + r * math.sin(a1)
        x0i, y0i = cx + inner * math.cos(a1), cy + inner * math.sin(a1)
        x1i, y1i = cx + inner * math.cos(a0), cy + inner * math.sin(a0)
        color = _CAT[i % len(_CAT)]
        # 2px surface gap between segments via a surface-colored stroke.
        parts.append(
            f'<path d="M {x0o:.2f} {y0o:.2f} A {r:.2f} {r:.2f} 0 {large} 1 '
            f'{x1o:.2f} {y1o:.2f} L {x0i:.2f} {y0i:.2f} A {inner:.2f} {inner:.2f} 0 '
            f'{large} 0 {x1i:.2f} {y1i:.2f} Z" fill="{color}" '
            f'stroke="var(--color-surface)" stroke-width="2"/>')
        a0 = a1
    parts.append(
        f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" font-size="19" '
        f'font-weight="700" font-family="var(--font-data)" fill="var(--color-ink)">'
        f'{_esc(_fmt(total, kind))}</text>')
    parts.append(
        f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" font-size="10" '
        f'fill="var(--color-mist)" letter-spacing="0.08em">TOTAL</text>')
    parts.append('</svg>')
    return "".join(parts)


def legend(slices, *, kind="money"):
    """Legend rows for a donut: colored swatch + label + value. Returns HTML."""
    slices = [(l, max(0, v or 0)) for l, v in slices if (v or 0) > 0]
    rows = []
    for i, (label, v) in enumerate(slices):
        color = _CAT[i % len(_CAT)]
        rows.append(
            f'<div class="flex items-center justify-between gap-3 py-1">'
            f'<span class="flex items-center gap-2 min-w-0">'
            f'<span class="h-3 w-3 shrink-0 rounded-sm" style="background:{color}"></span>'
            f'<span class="truncate text-ink">{_esc(label)}</span></span>'
            f'<span class="font-data text-mist">{_esc(_fmt(v, kind))}</span></div>')
    return "".join(rows)


def line_chart(points, *, kind="money", height=200, empty="No data"):
    """Single-series trend line (e.g. weekly revenue). points: list of
    (label, value). 2px line, markers, baseline; theme hue."""
    points = list(points)
    if len(points) < 2 or all((v or 0) <= 0 for _, v in points):
        return _empty(empty, height)
    vals = [max(0, v or 0) for _, v in points]
    vmax = max(vals) or 1
    pad_l, pad_r, pad_t, pad_b = 8, 8, 20, 30
    w = 760
    plot_w = w - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(points)
    y0 = pad_t + plot_h
    xs = [pad_l + (i / (n - 1)) * plot_w for i in range(n)]
    ys = [y0 - (v / vmax) * plot_h for v in vals]
    d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in zip(xs, ys))
    area = (f"M {xs[0]:.1f} {y0:.1f} L " +
            " L ".join(f"{x:.1f} {y:.1f}" for x, y in zip(xs, ys)) +
            f" L {xs[-1]:.1f} {y0:.1f} Z")
    parts = [f'<svg viewBox="0 0 {w} {height}" class="w-full" role="img" '
             f'preserveAspectRatio="xMidYMid meet" style="max-height:{height}px">']
    parts.append(f'<line x1="{pad_l}" y1="{y0}" x2="{w - pad_r}" y2="{y0}" '
                 f'stroke="var(--color-seam)" stroke-width="1"/>')
    parts.append(f'<path d="{area}" fill="var(--color-pool)" opacity="0.10"/>')
    parts.append(f'<path d="{d}" fill="none" stroke="var(--color-pool)" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (x, y) in enumerate(zip(xs, ys)):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" '
                     f'fill="var(--color-surface)" stroke="var(--color-pool)" stroke-width="2"/>')
        if i == 0 or i == n - 1:
            parts.append(
                f'<text x="{x:.1f}" y="{y0 + 15:.1f}" text-anchor="{"start" if i == 0 else "end"}" '
                f'font-size="10.5" fill="var(--color-mist)">{_esc(points[i][0])}</text>')
    parts.append('</svg>')
    return "".join(parts)


def _empty(msg, height):
    return (f'<div class="flex items-center justify-center rounded-lg border '
            f'border-dashed border-seam text-sm text-mist" '
            f'style="height:{height}px">{_esc(msg)}</div>')


def _truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def reading_chart(series, *, height=170):
    """Water-chemistry trend with the target band drawn behind the line.

    Unlike `line_chart`, the y-axis is the series' own range rather than a zero
    baseline: plotted from zero, the entire meaningful range of pH is a few
    pixels tall and every pool looks identical. The shaded band is the target
    +/- the dosing deadband, so "is this pool drifting" is answerable at a
    glance instead of by reading numbers.
    """
    points = list(series.points)
    if not points:
        return _empty("No readings yet", height)

    w, pad_l, pad_r, pad_t, pad_b = 760, 44, 10, 12, 26
    plot_w, plot_h = w - pad_l - pad_r, height - pad_t - pad_b
    lo, hi = float(series.axis_lo), float(series.axis_hi)
    span = (hi - lo) or 1.0

    def y_of(v):
        return pad_t + plot_h - ((float(v) - lo) / span) * plot_h

    n = len(points)
    xs = [pad_l + (i / (n - 1) if n > 1 else 0.5) * plot_w for i in range(n)]
    ys = [y_of(v) for _, v in points]

    parts = [f'<svg viewBox="0 0 {w} {height}" class="w-full" role="img" '
             f'preserveAspectRatio="xMidYMid meet" style="max-height:{height}px" '
             f'aria-label="{_esc(series.label)} over time">']

    if series.band_lo is not None:
        band_top, band_bottom = y_of(series.band_hi), y_of(series.band_lo)
        parts.append(
            f'<rect x="{pad_l}" y="{band_top:.1f}" width="{plot_w}" '
            f'height="{max(1.0, band_bottom - band_top):.1f}" '
            f'fill="var(--cr-green, #10b981)" opacity="0.12"/>')
        ty = y_of(series.target)
        parts.append(f'<line x1="{pad_l}" y1="{ty:.1f}" x2="{w - pad_r}" y2="{ty:.1f}" '
                     f'stroke="var(--cr-green, #10b981)" stroke-width="1" '
                     f'stroke-dasharray="4 3" opacity="0.8"/>')

    for value in (hi, lo):
        parts.append(
            f'<text x="{pad_l - 6}" y="{y_of(value) + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="var(--color-mist)">{_fmt_reading(value)}</text>')

    d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in zip(xs, ys))
    parts.append(f'<path d="{d}" fill="none" stroke="var(--color-pool)" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    for i, (x, y) in enumerate(zip(xs, ys)):
        value = points[i][1]
        outside = (series.band_lo is not None
                   and not (series.band_lo <= value <= series.band_hi))
        colour = "var(--color-safety)" if outside else "var(--color-pool)"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" '
                     f'fill="var(--color-surface)" stroke="{colour}" stroke-width="2"/>')

    for i in (0, n - 1) if n > 1 else (0,):
        anchor = "start" if i == 0 else "end"
        parts.append(
            f'<text x="{xs[i]:.1f}" y="{height - 4}" text-anchor="{anchor}" '
            f'font-size="11" fill="var(--color-mist)">'
            f'{_esc(points[i][0].strftime("%b %-d"))}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _fmt_reading(v) -> str:
    v = float(v)
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.1f}"

"""Regression: an RGBA (alpha) company logo must not render stretched.

reportlab wraps an alpha PNG in a Form XObject that ignores drawHeight, which
drew the 600x397 logo at 5.51in tall (a column down the page). pdf_logo flattens
to RGB and passes explicit width/height so both dimensions are honored. These
tests render a doc with the logo and measure the actual drawn image transform.
"""
import io
import re

from pypdf import PdfReader
from PIL import Image as PILImage
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate

from app import pdf_logo


def _make_rgba_logo(tmp_path):
    p = tmp_path / "logo.png"
    PILImage.new("RGBA", (600, 397), (10, 120, 200, 255)).save(p)
    return str(p)


def _drawn_image_sizes_in(pdf_bytes):
    """(width_in, height_in) of each drawn image XObject on page 1, from its cm matrix."""
    r = PdfReader(io.BytesIO(pdf_bytes))
    content = r.pages[0].get_contents().get_data().decode("latin-1")
    return [(float(m.group(1)) / 72, float(m.group(2)) / 72)
            for m in re.finditer(
                r"([-\d.]+) [-\d.]+ [-\d.]+ ([-\d.]+) [-\d.]+ [-\d.]+ cm\s+/[A-Za-z0-9_.]+ Do",
                content)]


def test_rgba_logo_is_not_stretched(tmp_path, monkeypatch):
    logo = _make_rgba_logo(tmp_path)
    monkeypatch.setattr("app.routers.company._logo_path", lambda: logo)
    img = pdf_logo.logo_flowable()
    assert img is not None
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter).build([img])
    sizes = _drawn_image_sizes_in(buf.getvalue())
    assert sizes, "no image drawn"
    w, h = sizes[0]
    assert h <= 1.0, f"logo drawn too tall ({h:.2f}in) — RGBA stretch regression"
    assert w <= 1.8, f"logo drawn too wide ({w:.2f}in)"
    # aspect ratio preserved (source 600x397 ~ 1.51)
    assert abs((w / h) - (600 / 397)) < 0.15, f"aspect distorted: {w:.2f}x{h:.2f}"


def test_logo_fits_within_the_box(tmp_path, monkeypatch):
    # A tall/narrow logo must be bounded by max_h, a wide one by max_w.
    p = tmp_path / "logo.png"
    PILImage.new("RGBA", (200, 1000), (0, 0, 0, 255)).save(p)  # very tall
    monkeypatch.setattr("app.routers.company._logo_path", lambda: str(p))
    img = pdf_logo.logo_flowable()
    w, h = img.wrap(6.5 * inch, 10 * inch)
    assert h <= 0.85 * inch + 1 and w <= 1.7 * inch + 1


def test_missing_logo_returns_none(monkeypatch):
    monkeypatch.setattr("app.routers.company._logo_path", lambda: "/nope/logo.png")
    assert pdf_logo.logo_flowable() is None

"""Shared company-logo flowable for the invoice + contract PDFs.

reportlab renders an RGBA (alpha) PNG as a Form XObject whose intrinsic height
ignores a post-construction ``drawHeight`` — so an alpha logo comes out stretched
to its full pixel height (a tall column running down the page). The fix: flatten
the logo onto a white background so it becomes a plain RGB image, and pass the
target ``width``/``height`` to the ``Image`` constructor (not set afterward) so
reportlab honors both. Aspect ratio is preserved and the result is capped to a
small header box.

Best-effort by contract: any failure (missing file, undecodable image, no PIL)
returns ``None`` so a bad logo never breaks a generated document.
"""
import io
import os

from reportlab.lib.units import inch
from reportlab.platypus import Image

from app import company


def logo_flowable(max_w: float = 1.7 * inch, max_h: float = 0.85 * inch):
    """Return a reportlab ``Image`` for the company logo, sized to fit within
    ``max_w`` x ``max_h`` while preserving aspect ratio, or ``None``."""
    try:
        from PIL import Image as PILImage

        # Module-attribute call (not a from-import of the name) so the test
        # suite's monkeypatch of app.company.logo_path still takes effect.
        path = company.logo_path()
        if not os.path.isfile(path):
            return None
        pil = PILImage.open(path)
        # Flatten any alpha onto white -> plain RGB, so reportlab uses an image
        # XObject that respects the size we pass rather than an alpha Form
        # XObject that ignores drawHeight.
        if pil.mode != "RGB":
            if pil.mode in ("RGBA", "LA", "PA"):
                pil = pil.convert("RGBA")
                bg = PILImage.new("RGB", pil.size, (255, 255, 255))
                bg.paste(pil, mask=pil.split()[-1])
                pil = bg
            else:
                pil = pil.convert("RGB")
        w, h = pil.size
        if w <= 0 or h <= 0:
            return None
        scale = min(max_w / w, max_h / h)
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        buf.seek(0)
        # width/height passed to the constructor (NOT set afterward) so both
        # dimensions are honored.
        return Image(buf, width=w * scale, height=h * scale)
    except Exception:
        return None

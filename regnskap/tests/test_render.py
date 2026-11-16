"""Page rendering stays bounded whatever page size a PDF declares."""

import io

import pypdfium2 as pdfium
import pytest
from PIL import Image

from src.accounts import render

A4 = (595, 842)


def _pdf(width: float, height: float) -> bytes:
    doc = pdfium.PdfDocument.new()
    doc.new_page(width, height)
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def test_a4_renders_at_full_dpi():
    img = render._render(_pdf(*A4), 0, render.DPI / 72)
    assert img.size == (round(A4[0] * render.DPI / 72), round(A4[1] * render.DPI / 72))


@pytest.mark.parametrize(
    "side",
    [render.MAX_RENDER_SIDE * 72 / render.DPI, 2384.0, 3456.0, 14400.0],
    ids=["at-ceiling", "a1-rounds-up", "2x-ceiling", "pdf-max"],
)
def test_bitmap_never_exceeds_ceiling(side):
    img = render._render(_pdf(side, side), 0, render.DPI / 72)
    assert max(img.size) <= render.MAX_RENDER_SIDE


def test_huge_page_still_gives_capped_outputs():
    pdf = _pdf(14400, 14400)
    assert max(Image.open(io.BytesIO(render.model_png(pdf, 1))).size) <= render.MAX_SIDE
    assert Image.open(io.BytesIO(render.view_webp(pdf, 1))).width == render.VIEW_WIDTH

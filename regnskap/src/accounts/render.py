"""PDF pages → images: PNG for the model (200 dpi gray, long side capped), WebP for the page viewer."""

import asyncio
import io
import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

DPI = 200
MAX_SIDE = 2400  # oversized pages render huge at 200 dpi; Gemini rejects > ~25 MP
VIEW_WIDTH = 1000
# Bitmap ceiling before the downscale: a PDF can declare 14400 pt pages (~1.6 GB at 200 dpi).
MAX_RENDER_SIDE = 2 * MAX_SIDE


def page_count(pdf: bytes) -> int:
    doc = pdfium.PdfDocument(pdf)
    try:
        return len(doc)
    finally:
        doc.close()


def _render(pdf: bytes, index: int, scale: float) -> Image.Image:
    doc = pdfium.PdfDocument(pdf)
    try:
        page = doc[index]
        # -0.5: pdfium rounds the bitmap size up, so an exact quotient can land one px over.
        scale = min(scale, (MAX_RENDER_SIDE - 0.5) / max(*page.get_size(), 1))
        return page.render(scale=scale, grayscale=True).to_pil()
    finally:
        doc.close()


def model_png(pdf: bytes, page: int) -> bytes:
    """1-based `page` as the PNG a page read gets."""
    img = _render(pdf, page - 1, DPI / 72)
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False)
    return out.getvalue()


def view_webp(pdf: bytes, page: int) -> bytes:
    """1-based `page` as a ~1000 px wide WebP for the scan viewer."""
    img = _render(pdf, page - 1, DPI / 72)
    if img.width > VIEW_WIDTH:
        img = img.resize((VIEW_WIDTH, round(img.height * VIEW_WIDTH / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=60)
    return out.getvalue()


# pdfium is not thread-safe: one render at a time, in a worker thread so the event loop stays free.
_lock = threading.Lock()


def _locked(fn, *args):
    with _lock:
        return fn(*args)


async def cached(pdf: bytes, page: int, path: Path, fn) -> bytes:
    """fn(pdf, page) rendered once and kept at `path`."""
    if path.exists():
        return path.read_bytes()
    data = await asyncio.to_thread(_locked, fn, pdf, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


async def count_pages(pdf: bytes) -> int:
    return await asyncio.to_thread(_locked, page_count, pdf)

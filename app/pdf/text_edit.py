"""Replace one existing text span. This does not reflow the paragraph."""

from __future__ import annotations

import os
import tempfile

import pymupdf

_BASE14 = {
    "Helvetica": "helv",
    "Helvetica-Bold": "hebo",
    "Helvetica-Oblique": "heit",
    "Helvetica-BoldOblique": "hebi",
    "Times-Roman": "tiro",
    "Times-Bold": "tibo",
    "Times-Italic": "tiit",
    "Times-BoldItalic": "tibi",
    "Courier": "cour",
    "Courier-Bold": "cobo",
    "Courier-Oblique": "coit",
    "Courier-BoldOblique": "cobi",
    "helv": "helv",
    "hebo": "hebo",
    "heit": "heit",
    "hebi": "hebi",
    "tiro": "tiro",
    "tibo": "tibo",
    "tiit": "tiit",
    "tibi": "tibi",
    "cour": "cour",
    "cobo": "cobo",
    "coit": "coit",
    "cobi": "cobi",
}
_CJK = {
    "china-s": "china-s",
    "china-ss": "china-ss",
    "china-t": "china-t",
    "china-ts": "china-ts",
    "Heiti": "china-s",
}
_FONT_SEQ = 0


def list_spans(page: pymupdf.Page) -> list[dict]:
    """Text spans in crop-local coordinates, plus a display rectangle."""
    rotation = page.rotation_matrix
    found = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text") or ""
                if not text.strip():
                    continue
                bbox = pymupdf.Rect(span["bbox"])
                found.append(
                    {
                        "text": text,
                        "bbox": bbox,
                        "display": pymupdf.Rect(bbox * rotation),
                        "size": float(span.get("size") or 12),
                        "font": span.get("font") or "",
                        "color": int(span.get("color") or 0),
                        "origin": pymupdf.Point(span.get("origin") or bbox.bl),
                    }
                )
    return found


def page_has_text(page: pymupdf.Page) -> bool:
    return bool(list_spans(page))


def replace_span(page: pymupdf.Page, span: dict, new_text: str) -> dict:
    """Cover the old glyphs and write ``new_text`` on the same baseline."""
    new_text = new_text.replace("\r", "").replace("\n", "")
    bbox = pymupdf.Rect(span["bbox"])
    redact = pymupdf.Rect(bbox.x0 - 0.35, bbox.y0 - 0.35, bbox.x1 + 0.35, bbox.y1 + 0.35)
    page.add_redact_annot(redact, fill=(1, 1, 1))
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    original_size = float(span["size"])
    if not new_text:
        return {
            "font_replaced": False,
            "fontsize": original_size,
            "original_size": original_size,
            "shrunk": False,
            "overflow": False,
            "cleared": True,
        }

    font, fontname, replaced, buffer = _pick_font(page, span, new_text)
    size = original_size
    width = max(bbox.width, 1.0)
    shrunk = False
    while size > 4 and font.text_length(new_text, fontsize=size) > width + 0.8:
        size = round(size - 0.25, 2)
        shrunk = True
    overflow = font.text_length(new_text, fontsize=size) > width + 0.8
    color = _color(int(span.get("color") or 0))
    origin = pymupdf.Point(span["origin"])
    if buffer is not None:
        _insert_with_buffer(page, origin, new_text, size, color, buffer)
    else:
        page.insert_text(origin, new_text, fontsize=size, fontname=fontname, color=color)
    return {
        "font_replaced": replaced,
        "fontsize": size,
        "original_size": original_size,
        "shrunk": shrunk or abs(size - original_size) > 0.05,
        "overflow": overflow,
        "cleared": False,
    }


def _pick_font(page: pymupdf.Page, span: dict, text: str):
    requested = span.get("font") or ""
    builtin_name = _BASE14.get(requested) or _CJK.get(requested)
    if builtin_name:
        builtin = pymupdf.Font(builtin_name)
        if _covers(builtin, text):
            return builtin, builtin_name, False, None

    if requested:
        for item in page.get_fonts(full=True):
            xref = item[0]
            basefont = item[3] if len(item) > 3 else ""
            internal = item[4] if len(item) > 4 else ""
            if not _same_font(requested, basefont, internal):
                continue
            try:
                extracted = page.parent.extract_font(xref)
                buffer = extracted[3] if extracted else None
                if not buffer:
                    continue
                font = pymupdf.Font(fontbuffer=buffer)
            except Exception:
                continue
            if _covers(font, text):
                return font, "Fedit", False, buffer

    fallback = pymupdf.Font("china-s")
    return fallback, "china-s", True, None


def _insert_with_buffer(page, origin, text, size, color, buffer: bytes) -> None:
    global _FONT_SEQ
    _FONT_SEQ += 1
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".ttf")
    try:
        handle.write(buffer)
        handle.close()
        page.insert_text(
            origin,
            text,
            fontsize=size,
            fontname=f"Fedit{_FONT_SEQ}",
            fontfile=handle.name,
            color=color,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _same_font(requested: str, basefont, internal) -> bool:
    for name in (basefont, internal):
        if not name:
            continue
        other = str(name)
        if requested == other:
            return True
        if len(requested) >= 3 and (requested in other or (len(other) >= 3 and other in requested)):
            return True
    return False


def _covers(font: pymupdf.Font, text: str) -> bool:
    for char in text:
        if char.isspace():
            continue
        try:
            if not font.has_glyph(ord(char)):
                return False
        except Exception:
            return False
    return True


def _color(value: int) -> tuple[float, float, float]:
    return ((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255

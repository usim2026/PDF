"""Place invoice pages onto A4 as vectors: 1, 2, or 4 per sheet."""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

A4 = pymupdf.paper_rect("a4")


def mm_to_pt(mm: float) -> float:
    return mm * 72.0 / 25.4


@dataclass
class ImposeSettings:
    n_up: int = 2
    margin_mm: float = 8.0
    gutter_mm: float = 6.0
    trim: bool = True
    cut_marks: bool = True
    scale: str = "fit"  # fit | actual
    orientation: str = "portrait"  # portrait | landscape


@dataclass
class ImposeResult:
    document: pymupdf.Document
    warnings: list[str] = field(default_factory=list)


def content_bounds(page: pymupdf.Page) -> pymupdf.Rect:
    """Tight visible-ink rectangle in ``page.rect`` space, or the full page."""
    full = pymupdf.Rect(page.rect)
    found = _ink_rect(page)
    if found is None:
        return full
    padded = pymupdf.Rect(found.x0 - 2, found.y0 - 2, found.x1 + 2, found.y1 + 2) & full
    if padded.width < 4 or padded.height < 4:
        return full
    if padded.width >= full.width * 0.98 and padded.height >= full.height * 0.98:
        return full
    return padded


def impose(src: pymupdf.Document, indices: list[int], settings: ImposeSettings) -> ImposeResult:
    if settings.n_up not in (1, 2, 4):
        raise ValueError("拼版只支持 1、2、4 张")
    if settings.scale not in ("fit", "actual"):
        raise ValueError("缩放方式无效")
    if settings.orientation not in ("portrait", "landscape"):
        raise ValueError("纸张方向无效")
    if not indices:
        raise ValueError("没有要拼版的页")
    if settings.margin_mm < 0 or settings.gutter_mm < 0:
        raise ValueError("边距不能为负数")

    margin = mm_to_pt(settings.margin_mm)
    gutter = mm_to_pt(settings.gutter_mm) if settings.n_up > 1 else 0.0
    paper = _paper_size(settings.orientation)
    slots = _slots(paper.width, paper.height, settings.n_up, margin, gutter)
    if any(slot.is_empty or slot.width < 20 or slot.height < 20 for slot in slots):
        raise ValueError("边距或间距太大，格子里放不下内容")

    normalized = _normalize_pages(src, indices)
    try:
        output = pymupdf.open()
        warnings: list[str] = []
        for start in range(0, normalized.page_count, settings.n_up):
            sheet = output.new_page(width=paper.width, height=paper.height)
            chunk = range(start, min(start + settings.n_up, normalized.page_count))
            for slot_index, src_index in enumerate(chunk):
                page = normalized[src_index]
                clip = content_bounds(page) if settings.trim else pymupdf.Rect(page.rect)
                dest, forced = _place_rect(clip, slots[slot_index], settings.scale)
                if forced:
                    original = indices[src_index] + 1
                    warnings.append(f"第 {original} 页按实际大小放不进格子，已改为适应格子。")
                sheet.show_pdf_page(dest, normalized, src_index, clip=clip, keep_proportion=True)
            if settings.cut_marks and settings.n_up > 1:
                _draw_cut_marks(sheet, settings.n_up, margin, gutter)
        return ImposeResult(document=output, warnings=warnings)
    finally:
        normalized.close()


def _normalize_pages(src: pymupdf.Document, indices: list[int]) -> pymupdf.Document:
    """Copy pages into a rotation-0 document that matches the on-screen view.

    ``show_pdf_page`` does not honor ``/Rotate``. Clearing the flag and passing
    the opposite angle reproduces 0/90/180/270, including a shifted crop box.
    """
    normalized = pymupdf.open()
    for pno in indices:
        page = src[pno]
        visual = pymupdf.Rect(page.rect)
        holder = pymupdf.open()
        try:
            holder.insert_pdf(src, from_page=pno, to_page=pno)
            holder[0].set_rotation(0)
            blank = normalized.new_page(width=visual.width, height=visual.height)
            blank.show_pdf_page(
                blank.rect,
                holder,
                0,
                rotate=(360 - page.rotation) % 360,
                keep_proportion=True,
            )
        finally:
            holder.close()
    return normalized


def _paper_size(orientation: str) -> pymupdf.Rect:
    if orientation == "landscape":
        return pymupdf.Rect(0, 0, A4.height, A4.width)
    return A4


def _slots(page_w: float, page_h: float, n_up: int, margin: float, gutter: float) -> list[pymupdf.Rect]:
    if n_up == 1:
        return [pymupdf.Rect(margin, margin, page_w - margin, page_h - margin)]
    if n_up == 2 and page_w > page_h:
        width = (page_w - 2 * margin - gutter) / 2
        height = page_h - 2 * margin
        left = pymupdf.Rect(margin, margin, margin + width, margin + height)
        right_x = margin + width + gutter
        right = pymupdf.Rect(right_x, margin, right_x + width, margin + height)
        return [left, right]
    if n_up == 2:
        height = (page_h - 2 * margin - gutter) / 2
        width = page_w - 2 * margin
        top = pymupdf.Rect(margin, margin, margin + width, margin + height)
        bottom_y = margin + height + gutter
        bottom = pymupdf.Rect(margin, bottom_y, margin + width, bottom_y + height)
        return [top, bottom]
    width = (page_w - 2 * margin - gutter) / 2
    height = (page_h - 2 * margin - gutter) / 2
    rects = []
    for row in range(2):
        for col in range(2):
            x0 = margin + col * (width + gutter)
            y0 = margin + row * (height + gutter)
            rects.append(pymupdf.Rect(x0, y0, x0 + width, y0 + height))
    return rects


def _place_rect(clip: pymupdf.Rect, slot: pymupdf.Rect, scale_mode: str) -> tuple[pymupdf.Rect, bool]:
    if clip.width <= 0 or clip.height <= 0:
        raise ValueError("页面没有可放置的内容")
    forced = False
    fits = clip.width <= slot.width + 0.5 and clip.height <= slot.height + 0.5
    if scale_mode == "actual" and fits:
        scale = 1.0
    else:
        scale = min(slot.width / clip.width, slot.height / clip.height)
        forced = scale_mode == "actual" and not fits
    placed_w = clip.width * scale
    placed_h = clip.height * scale
    x0 = slot.x0 + (slot.width - placed_w) / 2
    y0 = slot.y0 + (slot.height - placed_h) / 2
    return pymupdf.Rect(x0, y0, x0 + placed_w, y0 + placed_h), forced


def _draw_cut_marks(page: pymupdf.Page, n_up: int, margin: float, gutter: float) -> None:
    color = (0.45, 0.45, 0.45)
    if n_up == 2 and page.rect.width > page.rect.height:
        x = margin + ((page.rect.width - 2 * margin - gutter) / 2) + gutter / 2
        _stroke(page, pymupdf.Point(x, margin), pymupdf.Point(x, page.rect.height - margin), color)
        return
    if n_up == 2:
        y = margin + ((page.rect.height - 2 * margin - gutter) / 2) + gutter / 2
        _stroke(page, pymupdf.Point(margin, y), pymupdf.Point(page.rect.width - margin, y), color)
        return
    x = margin + ((page.rect.width - 2 * margin - gutter) / 2) + gutter / 2
    y = margin + ((page.rect.height - 2 * margin - gutter) / 2) + gutter / 2
    _stroke(page, pymupdf.Point(margin, y), pymupdf.Point(page.rect.width - margin, y), color)
    _stroke(page, pymupdf.Point(x, margin), pymupdf.Point(x, page.rect.height - margin), color)


def _stroke(page: pymupdf.Page, start: pymupdf.Point, end: pymupdf.Point, color: tuple[float, float, float]) -> None:
    shape = page.new_shape()
    shape.draw_line(start, end)
    shape.finish(color=color, dashes="[3 2] 0", width=0.7, closePath=False)
    shape.commit()


def _ink_rect(page: pymupdf.Page) -> pymupdf.Rect | None:
    zoom = 0.45
    if page.rect.width * zoom > 900:
        zoom = 900 / page.rect.width
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    samples = pixmap.samples
    width, height, channels = pixmap.width, pixmap.height, pixmap.n
    stride = pixmap.stride
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        row = y * stride
        for x in range(width):
            index = row + x * channels
            if samples[index] < 242 or samples[index + 1] < 242 or samples[index + 2] < 242:
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return None
    return pymupdf.Rect(min_x / zoom, min_y / zoom, (max_x + 1) / zoom, (max_y + 1) / zoom)


def ink_span_ratio(page: pymupdf.Page, slot: pymupdf.Rect) -> float:
    """Share of ``slot`` height that sits between the first and last ink rows."""
    if slot.height <= 0:
        return 0.0
    zoom = 0.5
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB, clip=slot)
    samples = pixmap.samples
    channels = pixmap.n
    stride = pixmap.stride
    first = last = None
    for y in range(pixmap.height):
        row = y * stride
        for x in range(pixmap.width):
            index = row + x * channels
            if samples[index] < 242 or samples[index + 1] < 242 or samples[index + 2] < 242:
                if first is None:
                    first = y
                last = y
                break
    if first is None or last is None:
        return 0.0
    return ((last - first + 1) / zoom) / slot.height

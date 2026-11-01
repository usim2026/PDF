"""Open, page edit, and save. The file on disk is never overwritten here."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf


class PdfError(Exception):
    """User-facing document error."""


class PasswordRequired(PdfError):
    """The file needs an open password."""


def open_pdf(data: bytes, password: str = "") -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfError("无法读取这份 PDF") from exc
    if doc.needs_pass and not doc.authenticate(password or ""):
        doc.close()
        raise PasswordRequired("需要打开密码")
    if doc.page_count < 1:
        doc.close()
        raise PdfError("PDF 里没有页面")
    return doc


def has_signature(doc: pymupdf.Document) -> bool:
    """True when the catalog says a signature exists."""
    try:
        flags = doc.get_sigflags()
    except Exception:
        return False
    return isinstance(flags, int) and flags >= 1


def visual_to_cropbox(page: pymupdf.Page, visual: pymupdf.Rect) -> pymupdf.Rect:
    """Map a rectangle drawn on the preview into CropBox coordinates.

    Preview pixels follow ``page.rect``. Text and ``set_cropbox`` use the
    unrotated crop-local space, then an origin shift into the media box.
    """
    local = pymupdf.Rect(pymupdf.Rect(visual) * page.derotation_matrix)
    crop = page.cropbox
    absolute = pymupdf.Rect(
        crop.x0 + local.x0,
        crop.y0 + local.y0,
        crop.x0 + local.x1,
        crop.y0 + local.y1,
    )
    absolute.normalize()
    return absolute & pymupdf.Rect(page.mediabox)


def rotate_pages(doc: pymupdf.Document, indices: list[int], delta: int) -> None:
    step = delta % 360
    if step not in (90, 180, 270):
        raise PdfError("只能旋转 90 度的倍数")
    for index in indices:
        page = doc[index]
        page.set_rotation((page.rotation + step) % 360)


def delete_pages(doc: pymupdf.Document, indices: list[int]) -> None:
    chosen = sorted(set(indices))
    if not chosen:
        raise PdfError("先选择要删除的页")
    if len(chosen) >= doc.page_count:
        raise PdfError("至少保留一页")
    for index in reversed(chosen):
        doc.delete_page(index)


def reorder_pages(doc: pymupdf.Document, order: list[int]) -> pymupdf.Document:
    if sorted(order) != list(range(doc.page_count)):
        raise PdfError("页面顺序无效")
    if order == list(range(doc.page_count)):
        return doc
    rebuilt = pymupdf.open()
    for index in order:
        rebuilt.insert_pdf(doc, from_page=index, to_page=index)
    return rebuilt


def crop_page(page: pymupdf.Page, visual: pymupdf.Rect) -> None:
    target = visual_to_cropbox(page, visual)
    if target.is_empty or target.width < 8 or target.height < 8:
        raise PdfError("裁切区域太小")
    page.set_cropbox(target)


def reset_crop(page: pymupdf.Page) -> None:
    page.set_cropbox(page.mediabox)


def extract_pages(doc: pymupdf.Document, indices: list[int]) -> bytes:
    if not indices:
        raise PdfError("没有要导出的页")
    piece = pymupdf.open()
    for index in indices:
        piece.insert_pdf(doc, from_page=index, to_page=index)
    data = piece.tobytes(garbage=3, deflate=True)
    piece.close()
    return data


def append_pdf(doc: pymupdf.Document, data: bytes, password: str = "") -> int:
    other = open_pdf(data, password)
    count = other.page_count
    doc.insert_pdf(other)
    other.close()
    return count


def _combine_pdfs(items: list[tuple[bytes, str | None, str]]) -> tuple[pymupdf.Document, list[int], list[str]]:
    combined = pymupdf.open()
    opened: list[pymupdf.Document] = []
    counts: list[int] = []
    digests: list[str] = []
    try:
        for data, _path, password in items:
            other = open_pdf(data, password)
            opened.append(other)
            counts.append(other.page_count)
            digests.append(hashlib.sha256(data).hexdigest())
            combined.insert_pdf(other)
        if combined.page_count < 1:
            raise PdfError("PDF 里没有页面")
    except Exception:
        combined.close()
        raise
    finally:
        for other in opened:
            other.close()
    return combined, counts, digests


def document_bytes(doc: pymupdf.Document) -> bytes:
    return doc.tobytes(garbage=3, deflate=True)


def assert_separate_path(target: str, source: str | None) -> None:
    if not source:
        return
    if Path(target).resolve() == Path(source).resolve():
        raise PdfError("原文件不会被覆盖，请换一个文件名另存")


class PdfSession:
    """In-memory working copy. ``source_bytes`` stays as opened."""

    def __init__(self) -> None:
        self.doc: pymupdf.Document | None = None
        self.source_path: str | None = None
        self.source_bytes: bytes | None = None
        self.sources: list[str] = []
        self.page_roles: list[str] = []
        self.page_file_ids: list[str] = []
        self.dirty = False

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()
            self.doc = None

    def load_bytes(self, data: bytes, path: str | None, password: str = "") -> None:
        doc = open_pdf(data, password)
        self.close()
        self.doc = doc
        self.source_path = path
        self.source_bytes = data
        self.sources = [path] if path else []
        self.page_roles = ["document"] * doc.page_count
        self.page_file_ids = [""] * doc.page_count
        self.dirty = False

    def clear(self) -> None:
        self.close()
        self.source_path = None
        self.source_bytes = None
        self.sources = []
        self.page_roles = []
        self.page_file_ids = []
        self.dirty = False

    def load_many(self, items: list[tuple[bytes, str | None, str]], role: str = "document") -> int:
        """Replace the working copy with these PDFs, in order. Returns the page count."""
        if not items:
            raise PdfError("没有要打开的 PDF")
        combined, counts, digests = _combine_pdfs(items)
        self.close()
        self.doc = combined
        only_one = len(items) == 1
        self.source_path = items[0][1] if only_one else None
        self.source_bytes = items[0][0] if only_one else None
        self.sources = [path for _data, path, _password in items if path]
        self.page_roles = []
        self.page_file_ids = []
        self._extend_page_meta(counts, digests, role)
        self.dirty = role == "invoice" or not only_one
        return combined.page_count

    def require(self) -> pymupdf.Document:
        if self.doc is None:
            raise PdfError("先打开一份 PDF")
        return self.doc

    def mark(self) -> None:
        self.dirty = True

    def rotate(self, indices: list[int], delta: int) -> None:
        rotate_pages(self.require(), indices, delta)
        self.mark()

    def delete(self, indices: list[int]) -> None:
        delete_pages(self.require(), indices)
        self._drop_page_meta(indices)
        self.mark()

    def reorder(self, order: list[int]) -> None:
        doc = self.require()
        rebuilt = reorder_pages(doc, order)
        if rebuilt is doc:
            return
        doc.close()
        self.doc = rebuilt
        self._permute_page_meta(order)
        self.mark()

    def crop(self, index: int, visual: pymupdf.Rect) -> None:
        crop_page(self.require()[index], visual)
        self.mark()

    def clear_crop(self, index: int) -> None:
        reset_crop(self.require()[index])
        self.mark()

    def merge(self, data: bytes, password: str = "") -> int:
        count = append_pdf(self.require(), data, password)
        self.mark()
        return count

    def merge_many(self, items: list[tuple[bytes, str, str | None]], role: str = "document") -> int:
        """Append PDFs without touching the working copy until every file opens."""
        doc = self.require()
        if not items:
            return 0
        normalized = [(data, path, password) for data, password, path in items]
        scratch, counts, digests = _combine_pdfs(normalized)
        added = scratch.page_count
        self._align_page_meta()
        try:
            if added:
                doc.insert_pdf(scratch)
        finally:
            scratch.close()
        if not added:
            return 0
        self._extend_page_meta(counts, digests, role)
        self.mark()
        for _data, _password, path in items:
            if path and path not in self.sources:
                self.sources.append(path)
        return added

    def _extend_page_meta(self, counts: list[int], digests: list[str], role: str) -> None:
        for count, digest in zip(counts, digests):
            self.page_roles.extend([role] * count)
            self.page_file_ids.extend([(digest if role == "invoice" else "")] * count)

    def _align_page_meta(self) -> None:
        count = self.doc.page_count if self.doc is not None else 0
        if len(self.page_roles) < count:
            self.page_roles.extend(["document"] * (count - len(self.page_roles)))
        if len(self.page_file_ids) < count:
            self.page_file_ids.extend([""] * (count - len(self.page_file_ids)))

    def _drop_page_meta(self, indices: list[int]) -> None:
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(self.page_roles):
                del self.page_roles[index]
            if 0 <= index < len(self.page_file_ids):
                del self.page_file_ids[index]

    def _permute_page_meta(self, order: list[int]) -> None:
        if len(self.page_roles) == len(order):
            self.page_roles = [self.page_roles[index] for index in order]
        if len(self.page_file_ids) == len(order):
            self.page_file_ids = [self.page_file_ids[index] for index in order]

    def assert_not_original(self, target: str) -> None:
        sources = list(self.sources)
        if self.source_path and self.source_path not in sources:
            sources.append(self.source_path)
        for source in sources:
            assert_separate_path(target, source)

    def save_to(self, path: str) -> None:
        self.assert_not_original(path)
        Path(path).write_bytes(document_bytes(self.require()))
        self.dirty = False

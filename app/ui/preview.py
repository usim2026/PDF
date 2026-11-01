"""Page preview: browse, drag a crop rectangle, or pick a text span."""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


def page_to_qimage(page: pymupdf.Page, zoom: float) -> QImage:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    image = QImage(pixmap.samples, pixmap.width, pixmap.height, pixmap.stride, QImage.Format.Format_RGB888)
    return image.copy()


class PreviewWidget(QWidget):
    resized = Signal()
    spanActivated = Signal(int)
    cropDrawn = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 420)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._page_rect: pymupdf.Rect | None = None
        self._spans: list[dict] = []
        self._mode = "browse"
        self._selected = -1
        self._crop: tuple[float, float, float, float] | None = None
        self._drag_origin: QPointF | None = None
        self._drag_current: QPointF | None = None

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._drag_origin = None
        self._drag_current = None
        if mode == "crop":
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == "text":
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_content(self, image: QImage | None, page_rect, spans: list[dict], reset: bool) -> None:
        self._image = image
        self._page_rect = pymupdf.Rect(page_rect) if page_rect is not None else None
        self._spans = spans
        if reset:
            self._selected = -1
            self._crop = None
            self._drag_origin = None
            self._drag_current = None
        self.update()

    def clear_crop(self) -> None:
        self._crop = None
        self._drag_origin = None
        self._drag_current = None
        self.update()

    def crop_rect(self) -> tuple[float, float, float, float] | None:
        return self._crop

    def selected_span(self) -> int:
        return self._selected

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor("#e7e9ee"))
        if self._image is None or self._page_rect is None:
            painter.setPen(QColor("#5c6570"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "打开 PDF，或先看一份示例发票")
            painter.end()
            return
        target = self._target()
        painter.fillRect(target.adjusted(-1, -1, 1, 1), QColor("#ffffff"))
        painter.drawImage(target, self._image)
        painter.setPen(QPen(QColor("#c5cad3"), 1))
        painter.drawRect(target)

        if self._mode == "text":
            for index, span in enumerate(self._spans):
                rect = self._page_to_widget(span["display"])
                if rect is None:
                    continue
                if index == self._selected:
                    painter.fillRect(rect, QColor(31, 78, 121, 48))
                    painter.setPen(QPen(QColor("#1f4e79"), 1.4))
                else:
                    painter.setPen(QPen(QColor(31, 78, 121, 90), 1))
                painter.drawRect(rect)

        crop = self._active_crop_widget()
        if crop is not None and self._mode == "crop":
            painter.fillRect(crop, QColor(20, 120, 80, 36))
            painter.setPen(QPen(QColor("#14784f"), 1.4, Qt.PenStyle.DashLine))
            painter.drawRect(crop)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._page_rect is None:
            return
        point = event.position()
        if self._mode == "crop":
            self._drag_origin = point
            self._drag_current = point
            self.update()
            return
        if self._mode == "text":
            hit = self._hit_span(point)
            self._selected = hit
            self.update()
            if hit >= 0:
                self.spanActivated.emit(hit)

    def mouseMoveEvent(self, event) -> None:
        if self._mode != "crop" or self._drag_origin is None:
            return
        self._drag_current = event.position()
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._mode != "crop" or self._drag_origin is None:
            return
        self._drag_current = event.position()
        widget_rect = self._rect_from_drag()
        self._drag_origin = None
        self._drag_current = None
        page_rect = self._widget_to_page(widget_rect) if widget_rect is not None else None
        if page_rect is None or page_rect.width < 8 or page_rect.height < 8:
            self._crop = None
            self.cropDrawn.emit(None)
        else:
            self._crop = (page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)
            self.cropDrawn.emit(self._crop)
        self.update()

    def _target(self) -> QRectF:
        assert self._image is not None
        ratio = self._image.devicePixelRatio() or 1
        logical_w = self._image.width() / ratio
        logical_h = self._image.height() / ratio
        available = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        scale = min(available.width() / logical_w, available.height() / logical_h)
        width = logical_w * scale
        height = logical_h * scale
        x = available.x() + (available.width() - width) / 2
        y = available.y() + (available.height() - height) / 2
        return QRectF(x, y, width, height)

    def _page_to_widget(self, rect) -> QRectF | None:
        if self._page_rect is None or self._page_rect.width <= 0 or self._page_rect.height <= 0:
            return None
        target = self._target()
        x0 = target.x() + (rect.x0 - self._page_rect.x0) / self._page_rect.width * target.width()
        y0 = target.y() + (rect.y0 - self._page_rect.y0) / self._page_rect.height * target.height()
        x1 = target.x() + (rect.x1 - self._page_rect.x0) / self._page_rect.width * target.width()
        y1 = target.y() + (rect.y1 - self._page_rect.y0) / self._page_rect.height * target.height()
        return QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    def _widget_to_page(self, rect: QRectF):
        if self._page_rect is None:
            return None
        target = self._target()
        if target.width() <= 0 or target.height() <= 0:
            return None

        def map_x(value: float) -> float:
            return self._page_rect.x0 + (value - target.x()) / target.width() * self._page_rect.width

        def map_y(value: float) -> float:
            return self._page_rect.y0 + (value - target.y()) / target.height() * self._page_rect.height

        mapped = pymupdf.Rect(map_x(rect.left()), map_y(rect.top()), map_x(rect.right()), map_y(rect.bottom()))
        mapped.normalize()
        return mapped & pymupdf.Rect(self._page_rect)

    def _rect_from_drag(self) -> QRectF | None:
        if self._drag_origin is None or self._drag_current is None:
            return None
        rect = QRectF(self._drag_origin, self._drag_current).normalized()
        if rect.width() < 4 or rect.height() < 4:
            return None
        return rect

    def _active_crop_widget(self) -> QRectF | None:
        if self._drag_origin is not None and self._drag_current is not None:
            return self._rect_from_drag()
        if self._crop is None or self._page_rect is None:
            return None
        return self._page_to_widget(pymupdf.Rect(*self._crop))

    def _hit_span(self, point: QPointF) -> int:
        if self._page_rect is None:
            return -1
        target = self._target()
        if not target.contains(point):
            return -1
        page_x = self._page_rect.x0 + (point.x() - target.x()) / target.width() * self._page_rect.width
        page_y = self._page_rect.y0 + (point.y() - target.y()) / target.height() * self._page_rect.height
        best = -1
        best_area = None
        for index, span in enumerate(self._spans):
            rect = pymupdf.Rect(span["display"])
            if rect.contains(pymupdf.Point(page_x, page_y)):
                area = rect.width * rect.height
                if best_area is None or area < best_area:
                    best = index
                    best_area = area
        return best

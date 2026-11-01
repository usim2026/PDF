"""Look at the imposed A4 sheets before saving or printing them."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.preview import PreviewWidget, page_to_qimage


class ImposePreviewDialog(QDialog):
    def __init__(self, document, warnings: list[str], on_save, on_print, on_rebuild, parent=None) -> None:
        super().__init__(parent)
        self.document = document
        self._on_save = on_save
        self._on_print = on_print
        self._on_rebuild = on_rebuild
        self._index = 0
        self._orientation = "portrait"
        self._switching = False
        self.setWindowTitle("拼版预览")
        self.resize(820, 900)

        layout = QVBoxLayout(self)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)
        self._set_warnings(warnings)

        direction = QWidget()
        direction_row = QHBoxLayout(direction)
        direction_row.setContentsMargins(0, 0, 0, 0)
        direction_row.addWidget(QLabel("纸张方向"))
        self.orientation_group = QButtonGroup(self)
        self.portrait_button = QRadioButton("竖版")
        self.landscape_button = QRadioButton("横版")
        self.orientation_group.addButton(self.portrait_button, 0)
        self.orientation_group.addButton(self.landscape_button, 1)
        self.portrait_button.setChecked(True)
        self.orientation_group.idClicked.connect(self._orientation_chosen)
        direction_row.addWidget(self.portrait_button)
        direction_row.addWidget(self.landscape_button)
        direction_row.addStretch(1)
        layout.addWidget(direction)

        self.preview = PreviewWidget()
        self.preview.set_mode("browse")
        layout.addWidget(self.preview, stretch=1)

        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.prev_button = QPushButton("上一张")
        self.next_button = QPushButton("下一张")
        self.page_label = QLabel()
        self.prev_button.clicked.connect(self._previous)
        self.next_button.clicked.connect(self._next)
        row.addWidget(self.prev_button)
        row.addWidget(self.next_button)
        row.addWidget(self.page_label, stretch=1)
        save_button = QPushButton("保存拼版 PDF")
        print_button = QPushButton("打印")
        close_button = QPushButton("关闭")
        save_button.clicked.connect(self._save)
        print_button.clicked.connect(self._print)
        close_button.clicked.connect(self.reject)
        row.addWidget(save_button)
        row.addWidget(print_button)
        row.addWidget(close_button)
        layout.addWidget(controls)
        self._show()

    def _show(self) -> None:
        count = self.document.page_count
        self._index = max(0, min(self._index, count - 1))
        page = self.document[self._index]
        width = max(self.preview.width(), 780)
        zoom = min(2.0, max(1.15, (width - 32) / page.rect.width))
        image = page_to_qimage(page, zoom)
        image.setDevicePixelRatio(1)
        self.preview.set_content(image, page.rect, [], reset=True)
        direction = "横版" if page.rect.width > page.rect.height else "竖版"
        self.page_label.setText(f"第 {self._index + 1} / {count} 张    {direction} A4")
        self.prev_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index + 1 < count)

    def _set_warnings(self, warnings: list[str]) -> None:
        if not warnings:
            self.warning_label.hide()
            self.warning_label.clear()
            return
        shown = warnings[:6]
        extra = len(warnings) - len(shown)
        text = "\n".join(shown)
        if extra > 0:
            text += f"\n另有 {extra} 条。"
        self.warning_label.setText(text)
        self.warning_label.show()

    def _orientation_chosen(self, button_id: int) -> None:
        if self._switching:
            return
        orientation = "landscape" if button_id == 1 else "portrait"
        if orientation == self._orientation:
            return
        self._switching = True
        try:
            result = self._on_rebuild(orientation)
        except Exception as exc:
            self._error(str(exc))
            self._sync_orientation_buttons()
            return
        finally:
            self._switching = False
        previous = self.document
        self.document = result.document
        previous.close()
        self._orientation = orientation
        self._set_warnings(result.warnings)
        if orientation == "landscape":
            self.resize(max(self.width(), 1080), 760)
        self._show()

    def _sync_orientation_buttons(self) -> None:
        button = self.landscape_button if self._orientation == "landscape" else self.portrait_button
        self._switching = True
        button.setChecked(True)
        self._switching = False

    def _error(self, text: str) -> None:
        QMessageBox.warning(self, "拼版预览", text)

    def done(self, code: int) -> None:
        document = self.document
        self.document = None
        if document is not None:
            document.close()
        super().done(code)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._show()

    def _previous(self) -> None:
        self._index -= 1
        self._show()

    def _next(self) -> None:
        self._index += 1
        self._show()

    def _save(self) -> None:
        self._on_save(self.document)

    def _print(self) -> None:
        self._on_print(self.document)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Left:
            self._previous()
            return
        if event.key() == Qt.Key.Key_Right:
            self._next()
            return
        super().keyPressEvent(event)

"""Confirm printer, copies, and imposition warnings before a job is sent."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)


class PrintDialog(QDialog):
    def __init__(
        self,
        printers: list[str],
        current: str,
        copies: int,
        sheets: int,
        warnings: list[str],
        parent=None,
        landscape: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("打印拼版")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        direction = "横版" if landscape else "竖版"
        layout.addWidget(QLabel(f"将打印 {sheets} 张 A4（{direction}）。页面按实际纸张铺满，不再额外缩小。"))

        form = QFormLayout()
        self.printers = QComboBox()
        self.printers.addItems(printers)
        if current:
            index = self.printers.findText(current)
            if index >= 0:
                self.printers.setCurrentIndex(index)
        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.setValue(max(1, copies))
        form.addRow("打印机", self.printers)
        form.addRow("份数", self.copies)
        layout.addLayout(form)

        if not printers:
            layout.addWidget(QLabel("没有检测到打印机。可以先保存拼版 PDF，再在系统里打印。"))
        if warnings:
            shown = warnings[:6]
            extra = len(warnings) - len(shown)
            text = "\n".join(shown)
            if extra > 0:
                text += f"\n另有 {extra} 条。"
            note = QLabel(text)
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("打印")
        ok_button.setEnabled(bool(printers))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def printer_name(self) -> str:
        return self.printers.currentText().strip()

    def copy_count(self) -> int:
        return int(self.copies.value())

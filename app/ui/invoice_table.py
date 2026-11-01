"""Review one row per invoice page, then save the workbook."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.pdf.invoice_fields import FIELDS, InvoiceRow, format_yuan, total_amount, write_invoice_xlsx


class InvoiceTableDialog(QDialog):
    def __init__(self, rows: list[InvoiceRow], suggested_path: str, parent=None, skipped: list[str] | None = None) -> None:
        super().__init__(parent)
        self._rows = rows
        self._suggested_path = suggested_path
        self.setWindowTitle("发票汇总")
        self.resize(980, 520)

        layout = QVBoxLayout(self)
        total, missing = total_amount(rows)
        recognized = sum(1 for row in rows if row.number)
        parts = [f"共 {len(rows)} 张发票，识别出号码 {recognized} 张。金额合计 {format_yuan(total)}。"]
        if missing:
            parts.append(f"其中 {missing} 张没有识别出金额，未计入。")
        if skipped:
            shown = skipped[:8]
            extra = len(skipped) - len(shown)
            text = "；".join(shown)
            if extra:
                text += f"；另有 {extra} 张"
            parts.append(f"已跳过 {len(skipped)} 张重复：{text}。")
        parts.append("只统计从「发票打印」导入的发票。重复号码不计入合计。项目明细没有拆开。")
        summary = QLabel("".join(parts))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.table = QTableWidget(len(rows), len(FIELDS))
        self.table.setHorizontalHeaderLabels([title for _key, title in FIELDS])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row_index, row in enumerate(rows):
            for column, (key, _title) in enumerate(FIELDS):
                self.table.setItem(row_index, column, QTableWidgetItem(row.value(key)))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        export_button = QPushButton("导出 Excel")
        export_button.clicked.connect(self._export)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(export_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出发票汇总", self._suggested_path, "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_invoice_xlsx(path, self._rows)
        except OSError as exc:
            QMessageBox.warning(self, "发票汇总", f"写不了文件：{exc}")
            return
        self._suggested_path = path
        QMessageBox.information(self, "发票汇总", f"已导出 {len(self._rows)} 行\n{path}")

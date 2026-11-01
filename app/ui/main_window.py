"""Three-pane window: page list, preview, and imposition controls."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from PySide6.QtCore import QDir, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeView,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.pdf.impose import ImposeSettings, impose
from app.pdf.invoice_fields import dedupe_invoice_rows, extract_invoice_rows, select_new_invoices
from app.pdf.ops import PasswordRequired, PdfError, PdfSession, extract_pages, has_signature, open_pdf
from app.pdf.print_job import default_printer_name, list_printers, print_document
from app.pdf.sample import make_sample_invoice
from app.pdf.text_edit import list_spans, replace_span
from app.ui.impose_preview import ImposePreviewDialog
from app.ui.invoice_table import InvoiceTableDialog
from app.ui.preview import PreviewWidget, page_to_qimage
from app.ui.print_dialog import PrintDialog


def natural_sort_key(name: str) -> list[int | str]:
    return [int(piece) if piece.isdigit() else piece.casefold() for piece in re.split(r"(\d+)", name)]


def desktop_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    # CSIDL_DESKTOPDIRECTORY: the real Desktop folder, not the virtual one.
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer) == 0 and buffer.value:
        return Path(buffer.value)
    return Path.home() / "Desktop"


def output_directory(desktop: Path) -> Path:
    folder = desktop / "PDF输出"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def pdfs_in_folder(folder: str) -> list[str]:
    """PDF files sitting directly in this folder, ordered by name."""
    root = Path(folder)
    files = [path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"]
    files.sort(key=lambda path: natural_sort_key(path.name))
    return [str(path) for path in files]


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("PDF 工具")
        self.session = PdfSession()
        self.settings = settings or QSettings("LocalPdf", "PdfTool")
        self._updating_list = False
        self._spans: list[dict] = []
        self._page_index = -1
        self._menu_actions: dict[str, QAction] = {}
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_preview)

        self._build_toolbar()
        self._build_body()
        self.setStatusBar(QStatusBar())
        self._load_settings()
        self._refresh_printers()
        self._show_page(-1)
        self.statusBar().showMessage("原文件只读。修改、拼版和打印都会写成新的 PDF。")

    def _build_toolbar(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        actions = [
            ("打开", self._open_file, QKeySequence.StandardKey.Open, "打开或追加 PDF。这里打开的不进入发票汇总"),
            ("发票打印", self._import_invoices, None, "导入要记账的发票。只有这里导入的才进入发票汇总，并检查重复"),
            ("清空", self._clear_documents, None, "清空当前打开的全部 PDF。有未保存的修改时会先询问"),
            ("另存为", self._save_as, QKeySequence.StandardKey.Save, None),
            ("导出所选页", self._export_selected, None, None),
            ("按页拆分", self._split_each, None, None),
            ("发票汇总", self._export_invoices, None, "只汇总「发票打印」导入的发票，重复号码不计入，并给出金额合计"),
        ]
        for text, slot, shortcut, tip in actions:
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            if tip:
                action.setToolTip(tip)
                action.setStatusTip(tip)
            action.triggered.connect(slot)
            self._menu_actions[text] = action
            bar.addAction(action)
        self._sync_menu()
        self.setAcceptDrops(True)

    def _build_body(self) -> None:
        splitter = QSplitter()
        self.setCentralWidget(splitter)

        self.pages = QListWidget()
        self.pages.setMinimumWidth(160)
        self.pages.setIconSize(QSize(110, 146))
        self.pages.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.pages.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.pages.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.pages.currentRowChanged.connect(self._on_current_row)
        self.pages.model().rowsMoved.connect(self._pages_reordered)
        splitter.addWidget(self.pages)

        self.preview = PreviewWidget()
        self.preview.resized.connect(self._schedule_render)
        self.preview.spanActivated.connect(self._span_chosen)
        self.preview.cropDrawn.connect(self._crop_drawn)
        splitter.addWidget(self.preview)

        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setFrameShape(QFrame.Shape.NoFrame)
        side.setMinimumWidth(280)
        side.setWidget(self._build_side())
        splitter.addWidget(side)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([190, 760, 320])

    def _build_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.signature_label = QLabel("这份 PDF 有数字签名。改字会破坏签名；拼版打印不改原文件。")
        self.signature_label.setWordWrap(True)
        self.signature_label.hide()
        layout.addWidget(self.signature_label)

        mode_box = QGroupBox("编辑模式")
        mode_row = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_browse = QRadioButton("浏览")
        self.mode_crop = QRadioButton("裁切")
        self.mode_text = QRadioButton("改字")
        self.mode_browse.setChecked(True)
        for index, button in enumerate((self.mode_browse, self.mode_crop, self.mode_text)):
            self.mode_group.addButton(button, index)
            mode_row.addWidget(button)
        self.mode_group.idClicked.connect(self._mode_changed)
        layout.addWidget(mode_box)

        pages_box = QGroupBox("页面")
        pages_layout = QVBoxLayout(pages_box)
        turn = QHBoxLayout()
        left = QPushButton("左转")
        right = QPushButton("右转")
        left.clicked.connect(lambda: self._rotate(-90))
        right.clicked.connect(lambda: self._rotate(90))
        turn.addWidget(left)
        turn.addWidget(right)
        pages_layout.addLayout(turn)
        remove = QPushButton("删除所选")
        remove.clicked.connect(self._delete_selected)
        pages_layout.addWidget(remove)
        crop_row = QHBoxLayout()
        self.apply_crop = QPushButton("应用裁切")
        self.apply_crop.setEnabled(False)
        self.apply_crop.clicked.connect(self._apply_crop)
        reset_crop = QPushButton("重置裁切")
        reset_crop.clicked.connect(self._reset_crop)
        crop_row.addWidget(self.apply_crop)
        crop_row.addWidget(reset_crop)
        pages_layout.addLayout(crop_row)
        crop_hint = QLabel("裁切模式下，在预览里拖一个矩形，再点应用。")
        crop_hint.setWordWrap(True)
        pages_layout.addWidget(crop_hint)
        layout.addWidget(pages_box)

        impose_box = QGroupBox("拼到 A4")
        form = QFormLayout(impose_box)
        nup_row = QHBoxLayout()
        self.nup_group = QButtonGroup(self)
        self.nup_buttons = {}
        for count in (1, 2, 4):
            button = QRadioButton(f"{count} 张")
            self.nup_group.addButton(button, count)
            self.nup_buttons[count] = button
            nup_row.addWidget(button)
        self.nup_buttons[2].setChecked(True)
        form.addRow("每张纸", nup_row)
        self.scope = QComboBox()
        self.scope.addItem("全部页面", "all")
        self.scope.addItem("选中的页", "selected")
        form.addRow("范围", self.scope)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 40)
        self.margin.setDecimals(1)
        self.margin.setSuffix(" mm")
        self.margin.setValue(8)
        form.addRow("边距", self.margin)
        self.gutter = QDoubleSpinBox()
        self.gutter.setRange(0, 40)
        self.gutter.setDecimals(1)
        self.gutter.setSuffix(" mm")
        self.gutter.setValue(6)
        form.addRow("间距", self.gutter)
        self.trim = QCheckBox("去掉周围空白")
        self.trim.setChecked(True)
        form.addRow("", self.trim)
        self.cut_marks = QCheckBox("画裁切线")
        self.cut_marks.setChecked(True)
        form.addRow("", self.cut_marks)
        self.scale = QComboBox()
        self.scale.addItem("适应格子", "fit")
        self.scale.addItem("实际大小", "actual")
        form.addRow("缩放", self.scale)
        preview_button = QPushButton("拼版预览")
        preview_button.clicked.connect(self._preview_impose)
        save_impose = QPushButton("保存拼版 PDF")
        save_impose.clicked.connect(self._save_impose)
        form.addRow(preview_button)
        form.addRow(save_impose)
        layout.addWidget(impose_box)

        print_box = QGroupBox("本地打印")
        print_form = QFormLayout(print_box)
        printer_row = QHBoxLayout()
        self.printers = QComboBox()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_printers)
        printer_row.addWidget(self.printers, stretch=1)
        printer_row.addWidget(refresh)
        print_form.addRow("打印机", printer_row)
        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.setValue(1)
        print_form.addRow("份数", self.copies)
        print_button = QPushButton("打印拼版")
        print_button.clicked.connect(self._print_impose)
        print_form.addRow(print_button)
        layout.addWidget(print_box)

        text_box = QGroupBox("改字")
        text_layout = QVBoxLayout(text_box)
        self.no_text = QLabel("这一页没有文字层，不能改字。扫描件需要以后单独做 OCR。")
        self.no_text.setWordWrap(True)
        self.no_text.hide()
        text_layout.addWidget(self.no_text)
        self.span_edit = QLineEdit()
        self.span_edit.setPlaceholderText("在改字模式下点选一段文字")
        text_layout.addWidget(self.span_edit)
        replace = QPushButton("替换这段文字")
        replace.clicked.connect(self._replace_text)
        text_layout.addWidget(replace)
        self.font_note = QLabel("只替换点中的那一段，不会把后面的字往后推。")
        self.font_note.setWordWrap(True)
        text_layout.addWidget(self.font_note)
        layout.addWidget(text_box)
        layout.addStretch(1)
        return panel

    def _load_settings(self) -> None:
        nup = self._as_int(self.settings.value("nup", 2), 2)
        if nup not in (1, 2, 4):
            nup = 2
        self.nup_buttons[nup].setChecked(True)
        self.margin.setValue(self._as_float(self.settings.value("margin", 8), 8))
        self.gutter.setValue(self._as_float(self.settings.value("gutter", 6), 6))
        self.trim.setChecked(self._as_bool(self.settings.value("trim", True), True))
        self.cut_marks.setChecked(self._as_bool(self.settings.value("cut_marks", True), True))
        scale = str(self.settings.value("scale", "fit"))
        index = self.scale.findData(scale)
        if index >= 0:
            self.scale.setCurrentIndex(index)
        scope = str(self.settings.value("scope", "all"))
        scope_index = self.scope.findData(scope)
        if scope_index >= 0:
            self.scope.setCurrentIndex(scope_index)
        self.copies.setValue(self._as_int(self.settings.value("copies", 1), 1))

    def _save_settings(self) -> None:
        self.settings.setValue("nup", self.nup_group.checkedId())
        self.settings.setValue("margin", self.margin.value())
        self.settings.setValue("gutter", self.gutter.value())
        self.settings.setValue("trim", self.trim.isChecked())
        self.settings.setValue("cut_marks", self.cut_marks.isChecked())
        self.settings.setValue("scale", self.scale.currentData())
        self.settings.setValue("scope", self.scope.currentData())
        self.settings.setValue("copies", self.copies.value())
        self.settings.setValue("printer", self.printers.currentText())
        self.settings.setValue("last_dir", self.settings.value("last_dir", ""))

    def _refresh_printers(self) -> None:
        current = self.printers.currentText() or str(self.settings.value("printer", ""))
        self.printers.clear()
        names = list_printers()
        self.printers.addItems(names)
        preferred = current or default_printer_name()
        index = self.printers.findText(preferred)
        if index >= 0:
            self.printers.setCurrentIndex(index)
        elif names:
            fallback = self.printers.findText(default_printer_name())
            if fallback >= 0:
                self.printers.setCurrentIndex(fallback)

    def dragEnterEvent(self, event) -> None:
        if self._content_mode() == "invoice":
            event.ignore()
            return
        if self._dropped_pdfs(event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self._dropped_pdfs(event.mimeData().urls())
        if paths:
            self._open_paths(paths)

    def _dropped_pdfs(self, urls) -> list[str]:
        paths: list[str] = []
        for url in urls:
            local = url.toLocalFile()
            if not local:
                continue
            candidate = Path(local)
            if candidate.is_dir():
                paths.extend(pdfs_in_folder(local))
            elif candidate.is_file() and candidate.suffix.lower() == ".pdf":
                paths.append(str(candidate))
        unique = list(dict.fromkeys(paths))
        unique.sort(key=lambda path: natural_sort_key(Path(path).name))
        return unique

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self._save_settings()
        self.session.close()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete and not isinstance(
            QApplication.focusWidget(), (QLineEdit, QSpinBox, QDoubleSpinBox)
        ):
            self._delete_selected()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.preview.clear_crop()
            self.apply_crop.setEnabled(False)
            return
        super().keyPressEvent(event)

    def _open_file(self) -> None:
        paths = self._choose_pdfs("打开 PDF")
        if paths:
            self._open_paths(paths)

    def _choose_pdfs(self, title: str) -> list[str]:
        """Multi-select PDFs, or import every PDF in the folder being viewed."""
        dialog, picked = self._pdf_dialog(title)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []
        if not picked:
            picked = [
                path
                for path in dialog.selectedFiles()
                if Path(path).is_file() and path.lower().endswith(".pdf")
            ]
            picked.sort(key=lambda path: natural_sort_key(Path(path).name))
        if picked:
            self.settings.setValue("last_dir", str(Path(picked[0]).parent))
        return picked

    def _pdf_dialog(self, title: str) -> tuple[QFileDialog, list[str]]:
        directory = str(self.settings.value("last_dir", ""))
        dialog = QFileDialog(self, title, directory, "PDF (*.pdf)")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setViewMode(QFileDialog.ViewMode.Detail)
        appending = self.session.doc is not None or title.startswith("合并")
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, "追加选中" if appending else "打开选中")
        dialog.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
        dialog.setLabelText(QFileDialog.DialogLabel.FileName, "文件名")
        dialog.setLabelText(QFileDialog.DialogLabel.FileType, "文件类型")
        dialog.setLabelText(QFileDialog.DialogLabel.LookIn, "位置")
        dialog.resize(1100, 640)
        self._attach_folder_tree(dialog, directory)
        picked: list[str] = []

        def import_folder() -> None:
            folder = dialog.directory().absolutePath()
            files = pdfs_in_folder(folder)
            if not files:
                QMessageBox.information(dialog, title, "这个文件夹里没有 PDF。只看这一层，不含子文件夹。")
                return
            picked[:] = files
            dialog.done(QDialog.DialogCode.Accepted)

        folder_button = QPushButton("导入此文件夹内全部 PDF", dialog)
        folder_button.setAutoDefault(False)
        folder_button.clicked.connect(import_folder)
        layout = dialog.layout()
        if layout is not None:
            layout.addWidget(folder_button, layout.rowCount(), 0, 1, max(1, layout.columnCount()))
        return dialog, picked

    def _attach_folder_tree(self, dialog: QFileDialog, directory: str) -> None:
        splitter = dialog.findChild(QSplitter, "splitter")
        if splitter is None:
            return
        model = QFileSystemModel(dialog)
        model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        model.setRootPath("")
        tree = QTreeView(dialog)
        tree.setObjectName("folderTree")
        tree.setModel(model)
        tree.setHeaderHidden(True)
        for column in range(1, model.columnCount()):
            tree.hideColumn(column)
        tree.setMinimumWidth(260)
        tree.setAnimated(False)
        replaced = splitter.replaceWidget(0, tree)
        if replaced is not None:
            replaced.hide()
            replaced.setParent(dialog)
        splitter.setCollapsible(0, False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root = model.index(model.myComputer())
        if root.isValid():
            tree.setRootIndex(root)

        def reveal(path: str) -> None:
            if not path:
                return
            dialog.setProperty("_reveal_path", path)
            target = Path(path)
            chain: list[Path] = []
            cursor = target
            while True:
                chain.append(cursor)
                parent = cursor.parent
                if parent == cursor:
                    break
                cursor = parent
            for folder in reversed(chain):
                index = model.index(str(folder))
                if index.isValid():
                    tree.expand(index)
            index = model.index(str(target))
            if index.isValid():
                tree.setCurrentIndex(index)
                tree.scrollTo(index)

        def on_loaded(_loaded: str) -> None:
            reveal(str(dialog.property("_reveal_path") or ""))

        def on_clicked(index) -> None:
            if dialog.property("_syncing_tree"):
                return
            path = model.filePath(index)
            if not path or not Path(path).is_dir():
                return
            dialog.setProperty("_syncing_tree", True)
            dialog.setDirectory(path)
            dialog.setProperty("_syncing_tree", False)

        def on_directory(path: str) -> None:
            if dialog.property("_syncing_tree"):
                return
            dialog.setProperty("_syncing_tree", True)
            reveal(path)
            dialog.setProperty("_syncing_tree", False)

        model.directoryLoaded.connect(on_loaded)
        tree.clicked.connect(on_clicked)
        dialog.directoryEntered.connect(on_directory)
        reveal(directory or dialog.directory().absolutePath())

        def widen() -> None:
            total = max(splitter.width(), dialog.width() - 40, 900)
            splitter.setSizes([280, max(420, total - 280)])

        QTimer.singleShot(0, widen)

    def _open_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        appending = self.session.doc is not None
        items = self._unlock_pdfs(paths)
        if not items:
            return
        try:
            if appending:
                added = self.session.merge_many([(data, password, path) for data, path, password in items])
            else:
                added = self.session.load_many(items)
        except PdfError as exc:
            self._error(str(exc))
            return
        if appending:
            self._reload_thumbs(self.pages.currentRow() if self.pages.currentRow() >= 0 else 0)
            self.statusBar().showMessage(f"已追加 {len(items)} 个 PDF，共 {added} 页")
            return
        self._after_load()
        if len(items) > 1:
            self.statusBar().showMessage(f"已打开 {len(items)} 个 PDF，共 {added} 页")

    def _unlock_pdfs(self, paths: list[str]) -> list[tuple[bytes, str | None, str]] | None:
        items: list[tuple[bytes, str | None, str]] = []
        for path in paths:
            name = Path(path).name
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                self._error(f"读不到 {name}：{exc}")
                return None
            password = self._password_for(data, name)
            if password is None:
                return None
            items.append((data, path, password))
        return items

    def _password_for(self, data: bytes, name: str) -> str | None:
        try:
            doc = open_pdf(data, "")
            doc.close()
            return ""
        except PasswordRequired:
            password, ok = QInputDialog.getText(
                self, "打开密码", f"{name} 需要密码：", QLineEdit.EchoMode.Password
            )
            if not ok:
                return None
            try:
                doc = open_pdf(data, password)
                doc.close()
                return password
            except PasswordRequired:
                self._error(f"密码不正确：{name}")
                return None
            except PdfError as exc:
                self._error(f"{name}：{exc}")
                return None
        except PdfError as exc:
            self._error(f"{name}：{exc}")
            return None

    def _load_sample(self) -> None:
        if not self._confirm_discard():
            return
        data = make_sample_invoice(4)
        self.session.load_bytes(data, None)
        self._after_load()
        self.statusBar().showMessage("已打开示例发票。上半部分是票面，下半部分是空白，适合看 2 张或 4 张拼版。")

    def _after_load(self) -> None:
        self._page_index = 0
        signed = has_signature(self.session.require())
        self.signature_label.setVisible(signed)
        self._reload_thumbs(0)
        self._update_title()

    def _import_invoices(self) -> None:
        paths = self._choose_pdfs("发票打印")
        if not paths:
            return
        items = self._unlock_pdfs(paths)
        if not items:
            return
        accepted, warnings = select_new_invoices(items, self._invoice_numbers(), self._invoice_file_ids())
        if warnings:
            self._warn("\n".join(warnings[:12]))
        if not accepted:
            self.statusBar().showMessage("没有导入新的发票")
            return
        try:
            if self.session.doc is None:
                added = self.session.load_many(accepted, role="invoice")
                self._after_load()
            else:
                added = self.session.merge_many(
                    [(data, password, path) for data, path, password in accepted],
                    role="invoice",
                )
                self._reload_thumbs(self.pages.currentRow() if self.pages.currentRow() >= 0 else 0)
        except (PdfError, PasswordRequired, OSError) as exc:
            self._error(str(exc))
            return
        self.statusBar().showMessage(f"已导入 {len(accepted)} 个发票 PDF，共 {added} 页")

    def _invoice_numbers(self) -> set[str]:
        doc = self.session.doc
        if doc is None:
            return set()
        numbers: set[str] = set()
        for row in extract_invoice_rows(doc, self._invoice_indices()):
            if row.number.strip():
                numbers.add(row.number.strip())
        return numbers

    def _invoice_file_ids(self) -> set[str]:
        return {
            file_id
            for role, file_id in zip(self.session.page_roles, self.session.page_file_ids)
            if role == "invoice" and file_id
        }

    def _invoice_indices(self) -> list[int]:
        return [index for index, role in enumerate(self.session.page_roles) if role == "invoice"]

    def _clear_documents(self) -> None:
        if self.session.doc is None:
            self.statusBar().showMessage("没有打开的 PDF")
            return
        if self.session.dirty:
            choice = self._ask_save_before_clear()
            if choice == "cancel":
                return
            if choice == "save" and not self._save_as():
                return
        else:
            answer = QMessageBox.question(
                self,
                "清空",
                "清空已打开的全部 PDF？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.session.clear()
        self._clear_view()
        self.statusBar().showMessage("已清空")

    def _ask_save_before_clear(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("清空")
        box.setText("这些修改还没另存。清空前要先保存吗？")
        save = box.addButton("保存并清空", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("不保存", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is save:
            return "save"
        if box.clickedButton() is cancel:
            return "cancel"
        return "discard"

    def _clear_view(self) -> None:
        self._page_index = -1
        self._spans = []
        self.pages.clear()
        self.signature_label.hide()
        self.preview.clear_crop()
        self.apply_crop.setEnabled(False)
        self._show_page(-1)
        self._update_title()

    def _save_as(self) -> bool:
        try:
            self.session.require()
        except PdfError as exc:
            self._error(str(exc))
            return False
        path, _ = QFileDialog.getSaveFileName(self, "另存为", self._suggest_path("已编辑"), "PDF (*.pdf)")
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self.session.save_to(path)
        except (PdfError, OSError) as exc:
            self._error(str(exc))
            return False
        self._update_title()
        self.statusBar().showMessage(f"已另存 {path}")
        return True

    def _export_invoices(self) -> None:
        try:
            doc = self.session.require()
        except PdfError as exc:
            self._error(str(exc))
            return
        indices = self._invoice_indices()
        if not indices:
            self._error("还没有用「发票打印」导入发票。从「打开」进来的 PDF 不计入汇总。")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rows = extract_invoice_rows(doc, indices)
            kept, skipped = dedupe_invoice_rows(rows)
        finally:
            QApplication.restoreOverrideCursor()
        if not kept:
            self._error("这些发票页没有识别出可汇总的内容。")
            return
        InvoiceTableDialog(kept, self._suggest_workbook(), self, skipped).exec()

    def _suggest_workbook(self) -> str:
        return str(self._output_dir() / "发票汇总.xlsx")

    def _export_selected(self) -> None:
        try:
            doc = self.session.require()
            indices = self._explicit_selection()
            if not indices:
                raise PdfError("先在左侧选中要导出的页")
            path, _ = QFileDialog.getSaveFileName(self, "导出所选页", self._suggest_path("节选"), "PDF (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self.session.assert_not_original(path)
            Path(path).write_bytes(extract_pages(doc, indices))
        except (PdfError, OSError) as exc:
            self._error(str(exc))
            return
        self.statusBar().showMessage(f"已导出 {len(indices)} 页")

    def _split_each(self) -> None:
        try:
            doc = self.session.require()
        except PdfError as exc:
            self._error(str(exc))
            return
        directory = QFileDialog.getExistingDirectory(self, "选择拆分后的文件夹", str(self._output_dir()))
        if not directory:
            return
        stem = Path(self.session.source_path).stem if self.session.source_path else "页面"
        written = 0
        try:
            for index in range(doc.page_count):
                target = str(Path(directory) / f"{stem}-{index + 1}.pdf")
                self.session.assert_not_original(target)
                Path(target).write_bytes(extract_pages(doc, [index]))
                written += 1
        except (PdfError, OSError) as exc:
            self._error(str(exc))
            return
        self.statusBar().showMessage(f"已拆成 {written} 个文件")

    def _rotate(self, delta: int) -> None:
        try:
            indices = self._selected_or_current()
            self.session.rotate(indices, delta)
        except PdfError as exc:
            self._error(str(exc))
            return
        self._reload_thumbs(self.pages.currentRow())

    def _delete_selected(self) -> None:
        try:
            indices = self._explicit_selection() or self._selected_or_current()
            if len(indices) > 1:
                answer = QMessageBox.question(self, "删除页面", f"删除选中的 {len(indices)} 页？这只影响副本。")
                if answer != QMessageBox.StandardButton.Yes:
                    return
            current = self.pages.currentRow()
            self.session.delete(indices)
        except PdfError as exc:
            self._error(str(exc))
            return
        next_row = min(current, self.session.doc.page_count - 1)
        self._reload_thumbs(max(0, next_row))

    def _apply_crop(self) -> None:
        rect = self.preview.crop_rect()
        if rect is None or self._page_index < 0:
            self._error("先在预览里拖出裁切矩形")
            return
        try:
            self.session.crop(self._page_index, pymupdf.Rect(*rect))
        except PdfError as exc:
            self._error(str(exc))
            return
        self.preview.clear_crop()
        self.apply_crop.setEnabled(False)
        self._reload_thumbs(self._page_index)

    def _reset_crop(self) -> None:
        if self._page_index < 0:
            return
        try:
            self.session.clear_crop(self._page_index)
        except PdfError as exc:
            self._error(str(exc))
            return
        self.preview.clear_crop()
        self.apply_crop.setEnabled(False)
        self._reload_thumbs(self._page_index)

    def _preview_impose(self) -> None:
        try:
            result = self._build_impose()
        except (PdfError, ValueError) as exc:
            self._error(str(exc))
            return
        dialog = ImposePreviewDialog(
            result.document,
            result.warnings,
            self._save_document,
            self._print_document,
            self._build_impose,
            self,
        )
        dialog.exec()

    def _save_impose(self) -> None:
        try:
            result = self._build_impose()
        except (PdfError, ValueError) as exc:
            self._error(str(exc))
            return
        try:
            self._save_document(result.document)
            self._show_warnings(result.warnings)
        finally:
            result.document.close()

    def _print_impose(self) -> None:
        try:
            result = self._build_impose()
        except (PdfError, ValueError) as exc:
            self._error(str(exc))
            return
        try:
            self._print_document(result.document, result.warnings)
        finally:
            result.document.close()

    def _save_document(self, document) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存拼版 PDF", self._suggest_path("拼版"), "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self.session.assert_not_original(path)
            Path(path).write_bytes(document.tobytes(garbage=3, deflate=True))
        except (PdfError, OSError) as exc:
            self._error(str(exc))
            return
        self.statusBar().showMessage(f"拼版已保存 {path}")

    def _print_document(self, document, warnings: list[str] | None = None) -> None:
        self._refresh_printers()
        dialog = PrintDialog(
            list_printers(),
            self.printers.currentText(),
            self.copies.value(),
            document.page_count,
            warnings or [],
            self,
            landscape=document.page_count > 0 and document[0].rect.width > document[0].rect.height,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.copies.setValue(dialog.copy_count())
        index = self.printers.findText(dialog.printer_name())
        if index >= 0:
            self.printers.setCurrentIndex(index)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            print_document(document, dialog.printer_name(), dialog.copy_count())
        except RuntimeError as exc:
            self._error(str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._save_settings()
        self.statusBar().showMessage(f"已发送到 {dialog.printer_name()}")

    def _replace_text(self) -> None:
        if self._page_index < 0 or self.preview.selected_span() < 0 or self.preview.selected_span() >= len(self._spans):
            self._error("先切换到改字模式，再点选一段文字")
            return
        new_text = self.span_edit.text()
        span = self._spans[self.preview.selected_span()]
        if new_text == span["text"]:
            return
        doc = self.session.require()
        if has_signature(doc) and not self._confirm_signature_edit():
            return
        try:
            result = replace_span(doc[self._page_index], span, new_text)
        except Exception as exc:
            self._error(f"没能改这段字：{exc}")
            return
        self.session.mark()
        self.font_note.setText(self._replace_message(result))
        self._reload_thumbs(self._page_index)

    def _build_impose(self, orientation: str = "portrait"):
        self._save_settings()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            settings = ImposeSettings(
                n_up=self.nup_group.checkedId(),
                margin_mm=self.margin.value(),
                gutter_mm=self.gutter.value(),
                trim=self.trim.isChecked(),
                cut_marks=self.cut_marks.isChecked(),
                scale=self.scale.currentData(),
                orientation=orientation,
            )
            return impose(self.session.require(), self._impose_indices(), settings)
        finally:
            QApplication.restoreOverrideCursor()

    def _impose_indices(self) -> list[int]:
        doc = self.session.require()
        if self.scope.currentData() == "selected":
            rows = self._explicit_selection()
            if not rows:
                raise PdfError("先在左侧选中要拼版的页")
            return rows
        return list(range(doc.page_count))

    def _reload_thumbs(self, current: int) -> None:
        doc = self.session.doc
        if doc is None:
            return
        self._updating_list = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.pages.clear()
            for index, page in enumerate(doc):
                zoom = 110 / max(page.rect.width, 1)
                image = page_to_qimage(page, zoom)
                item = QListWidgetItem(f"第 {index + 1} 页")
                item.setIcon(QIcon(QPixmap.fromImage(image)))
                item.setData(Qt.ItemDataRole.UserRole, index)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                )
                self.pages.addItem(item)
            if self.pages.count():
                row = max(0, min(current, self.pages.count() - 1))
                self.pages.setCurrentRow(row)
                self._page_index = row
        finally:
            self._updating_list = False
            QApplication.restoreOverrideCursor()
        self._show_page(self.pages.currentRow(), reset=True)
        self._update_title()

    def _show_page(self, index: int, reset: bool = True) -> None:
        self._page_index = index
        if self.session.doc is None or index < 0 or index >= self.session.doc.page_count:
            self._spans = []
            self.preview.set_content(None, None, [], reset=True)
            self.no_text.hide()
            self.statusBar().showMessage("打开一份 PDF 开始")
            return
        page = self.session.doc[index]
        self._spans = list_spans(page)
        self.no_text.setVisible(self.mode_text.isChecked() and not self._spans)
        self._render_preview(reset=reset)
        width_mm = page.rect.width * 25.4 / 72
        height_mm = page.rect.height * 25.4 / 72
        dirty = "已修改" if self.session.dirty else "未修改原文件"
        self.statusBar().showMessage(
            f"第 {index + 1}/{self.session.doc.page_count} 页    {width_mm:.0f} × {height_mm:.0f} mm    {dirty}"
        )

    def _render_preview(self, reset: bool = False) -> None:
        if self.session.doc is None or self._page_index < 0:
            return
        if self._page_index >= self.session.doc.page_count:
            return
        page = self.session.doc[self._page_index]
        target = max(240, self.preview.width() - 32)
        ratio = self.preview.devicePixelRatioF() or 1
        zoom = min(3.0, target / max(page.rect.width, 1) * ratio)
        image = page_to_qimage(page, zoom)
        image.setDevicePixelRatio(ratio)
        self._spans = list_spans(page)
        self.preview.set_content(image, page.rect, self._spans, reset=reset)

    def _schedule_render(self) -> None:
        if self.session.doc is not None:
            self._render_timer.start(90)

    def _on_current_row(self, row: int) -> None:
        if self._updating_list:
            return
        self.span_edit.clear()
        self.font_note.setText("只替换点中的那一段，不会把后面的字往后推。")
        self.apply_crop.setEnabled(False)
        self._show_page(row, reset=True)

    def _pages_reordered(self, *_) -> None:
        if self._updating_list or self.session.doc is None:
            return
        order = [self.pages.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.pages.count())]
        if order == list(range(len(order))):
            return
        current_item = self.pages.currentItem()
        current_old = current_item.data(Qt.ItemDataRole.UserRole) if current_item else 0
        try:
            self.session.reorder(order)
        except PdfError as exc:
            self._error(str(exc))
            self._reload_thumbs(0)
            return
        new_index = order.index(current_old) if current_old in order else 0
        self._reload_thumbs(new_index)

    def _mode_changed(self, _mode_id: int) -> None:
        if self.mode_crop.isChecked():
            mode = "crop"
        elif self.mode_text.isChecked():
            mode = "text"
        else:
            mode = "browse"
        self.preview.set_mode(mode)
        self.no_text.setVisible(mode == "text" and self.session.doc is not None and not self._spans)

    def _span_chosen(self, index: int) -> None:
        if index < 0 or index >= len(self._spans):
            return
        self.span_edit.setText(self._spans[index]["text"])
        self.span_edit.setFocus()

    def _crop_drawn(self, rect) -> None:
        self.apply_crop.setEnabled(rect is not None)

    def _selected_or_current(self) -> list[int]:
        chosen = self._explicit_selection()
        if chosen:
            return chosen
        if self.pages.currentRow() >= 0:
            return [self.pages.currentRow()]
        raise PdfError("先打开 PDF")

    def _explicit_selection(self) -> list[int]:
        return sorted({index.row() for index in self.pages.selectedIndexes()})

    def _confirm_discard(self) -> bool:
        if not self.session.dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("还有未另存的修改")
        box.setText("这些修改还没另存。继续会丢掉副本里的修改，磁盘上的原文件仍然没变。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("继续")
        box.button(QMessageBox.StandardButton.No).setText("留下")
        return box.exec() == QMessageBox.StandardButton.Yes

    def _confirm_signature_edit(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("签名会失效")
        box.setText("这份 PDF 有数字签名。改字会破坏签名，票面如果被改过，二维码和税局记录也不会跟着变。")
        box.setInformativeText("继续只改内存里的副本，不会覆盖原文件。")
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Ok).setText("继续修改")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        return box.exec() == QMessageBox.StandardButton.Ok

    def _replace_message(self, result: dict) -> str:
        if result.get("cleared"):
            return "这段文字已去掉。"
        parts = []
        if result.get("font_replaced"):
            parts.append("已换字体：原字体里没有这些字，已用内置简体写出，字宽可能略有差别。")
        else:
            parts.append("已写回。")
        if result.get("shrunk"):
            parts.append(f"字号从 {result['original_size']:.1f} 调到 {result['fontsize']:.1f}。")
        if result.get("overflow"):
            parts.append("新文字仍比原来宽，可能会挨到旁边的字。")
        return "".join(parts)

    def _show_warnings(self, warnings: list[str]) -> None:
        if not warnings:
            return
        QMessageBox.information(self, "拼版提示", "\n".join(warnings[:8]))

    def _output_dir(self) -> Path:
        return output_directory(desktop_directory())

    def _suggest_path(self, suffix: str) -> str:
        directory = self._output_dir()
        if len(self.session.sources) > 1:
            stem = Path(self.session.sources[0]).parent.name or "发票"
            return str(directory / f"{stem}-{suffix}.pdf")
        if self.session.source_path:
            stem = Path(self.session.source_path).stem
            return str(directory / f"{stem}-{suffix}.pdf")
        return str(directory / f"未命名-{suffix}.pdf")

    def _update_title(self) -> None:
        if len(self.session.sources) > 1:
            name = f"{len(self.session.sources)} 个 PDF"
        elif self.session.source_path:
            name = Path(self.session.source_path).name
        else:
            name = "未命名"
        mark = "*" if self.session.dirty else ""
        if self.session.doc is None:
            self.setWindowTitle("PDF 工具")
        else:
            self.setWindowTitle(f"{mark}{name} - PDF 工具")
        self._sync_menu()

    def _content_mode(self) -> str:
        if self.session.doc is None or not self.session.page_roles:
            return "empty"
        if any(role == "invoice" for role in self.session.page_roles):
            return "invoice"
        return "document"

    def _sync_menu(self) -> None:
        if not self._menu_actions:
            return
        mode = self._content_mode()
        invoice = mode == "invoice"
        document = mode == "document"
        opened = mode != "empty"
        self._menu_actions["打开"].setEnabled(not invoice)
        self._menu_actions["另存为"].setEnabled(document)
        self._menu_actions["导出所选页"].setEnabled(document)
        self._menu_actions["按页拆分"].setEnabled(document)
        self._menu_actions["发票打印"].setEnabled(not document)
        self._menu_actions["发票汇总"].setEnabled(invoice)
        self._menu_actions["清空"].setEnabled(opened)

    def _error(self, text: str) -> None:
        QMessageBox.warning(self, "PDF 工具", text)

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, "发票打印", text)

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).lower() in {"1", "true", "yes"}

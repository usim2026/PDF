"""Core checks for page edits, A4 imposition, text replacement, and print painting."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QSplitter, QTreeView

from app.pdf.impose import ImposeSettings, _slots, content_bounds, impose, ink_span_ratio, mm_to_pt
from app.pdf.invoice_fields import (
    dedupe_invoice_rows,
    extract_invoice_rows,
    select_new_invoices,
    total_amount,
    write_invoice_xlsx,
)
from app.pdf.ops import (
    PdfError,
    PdfSession,
    append_pdf,
    crop_page,
    delete_pages,
    extract_pages,
    has_signature,
    open_pdf,
    reorder_pages,
)
from app.pdf.print_job import list_printers, print_document, print_to_pdf
from app.pdf.sample import make_sample_invoice
from app.ui.impose_preview import ImposePreviewDialog
from app.ui.invoice_table import InvoiceTableDialog
from app.pdf.text_edit import list_spans, replace_span
from app.ui.main_window import MainWindow, output_directory, pdfs_in_folder


def _digital_invoice() -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        (48, 48, "电子发票（增值税专用发票）"),
        (48, 80, "发票号码：26112000000012345678"),
        (320, 80, "开票日期：2026年03月15日"),
        (48, 120, "购买方信息"),
        (320, 120, "销售方信息"),
        (48, 150, "名称：北京示例科技有限公司"),
        (320, 150, "名称：上海示例贸易有限公司"),
        (48, 180, "统一社会信用代码/纳税人识别号：91110000MA0000000X"),
        (320, 180, "统一社会信用代码/纳税人识别号：91310000MA0000000Y"),
        (48, 230, "项目名称 金额 税率 税额"),
        (48, 260, "*现代服务*技术服务 1000.00 6% 60.00"),
        (48, 310, "合  计"),
        (300, 310, "¥1000.00"),
        (430, 310, "¥60.00"),
        (48, 350, "价税合计（大写）⊗壹仟零陆拾圆整"),
        (320, 350, "（小写）¥1060.00"),
    ]
    for left, top, value in lines:
        page.insert_text((left, top), value, fontsize=11, fontname="china-s")
    return doc


def _labeled_page(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontname="helv", fontsize=16)
    data = doc.tobytes()
    doc.close()
    return data


def _ink(page: pymupdf.Page) -> pymupdf.Rect | None:
    return content_bounds(page)


def _has_axis_line(page: pymupdf.Page, axis: str) -> bool:
    mid_x = page.rect.width / 2
    mid_y = page.rect.height / 2
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        if axis == "horizontal" and abs((rect.y0 + rect.y1) / 2 - mid_y) < 4 and rect.width > 300 and rect.height < 4:
            return True
        if axis == "vertical" and abs((rect.x0 + rect.x1) / 2 - mid_x) < 4 and rect.height > 300 and rect.width < 4:
            return True
    return False


class PdfCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_original_file_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.pdf"
            path.write_bytes(make_sample_invoice(2))
            before = path.read_bytes()
            session = PdfSession()
            session.load_bytes(before, str(path))
            session.rotate([0], 90)
            session.crop(0, pymupdf.Rect(10, 10, 120, 120))
            spans = list_spans(session.doc[1])
            self.assertTrue(spans)
            replace_span(session.doc[1], spans[0], "已改")
            session.mark()
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(Exception):
                session.save_to(str(path))
            target = Path(folder) / "copy.pdf"
            session.save_to(str(target))
            self.assertEqual(path.read_bytes(), before)
            reopened = open_pdf(target.read_bytes())
            self.assertGreaterEqual(reopened.page_count, 1)
            reopened.close()
            session.close()

    def test_page_ops_reorder_merge_split_rotate_delete(self):
        doc = open_pdf(make_sample_invoice(3))
        self.assertIn("INVNO0001", doc[0].get_text("text"))
        width, height = doc[0].rect.width, doc[0].rect.height
        doc[0].set_rotation(90)
        self.assertEqual(doc[0].rect.width, height)
        self.assertEqual(doc[0].rect.height, width)
        doc[0].set_rotation(0)

        rebuilt = reorder_pages(doc, [2, 0, 1])
        self.assertIn("INVNO0003", rebuilt[0].get_text("text"))
        self.assertIn("INVNO0001", rebuilt[1].get_text("text"))
        extra = open_pdf(make_sample_invoice(1))
        extra_bytes = extra.tobytes()
        extra.close()
        added = append_pdf(rebuilt, extra_bytes)
        self.assertEqual(added, 1)
        self.assertEqual(rebuilt.page_count, 4)
        piece = extract_pages(rebuilt, [0, 2])
        piece_doc = open_pdf(piece)
        self.assertEqual(piece_doc.page_count, 2)
        piece_doc.close()
        delete_pages(rebuilt, [3])
        self.assertEqual(rebuilt.page_count, 3)
        rebuilt.close()
        doc.close()

    def test_crop_keeps_ink_and_drops_margin(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=600)
        page.draw_rect(pymupdf.Rect(80, 100, 180, 220), fill=(0, 0, 0))
        page.set_rotation(90)
        before = pymupdf.Rect(page.rect)
        ink = content_bounds(page)
        crop_page(page, ink)
        self.assertLess(page.rect.width * page.rect.height, before.width * before.height * 0.5)
        pixmap = page.get_pixmap(alpha=False)
        self.assertLess(min(pixmap.samples), 20)
        doc.close()

    def test_sample_trim_is_tighter_than_full_page(self):
        doc = open_pdf(make_sample_invoice(1))
        bounds = content_bounds(doc[0])
        self.assertLess(bounds.height, doc[0].rect.height * 0.6)
        self.assertGreater(bounds.height, 250)
        doc.close()

    def test_two_and_four_up_are_vector_a4(self):
        doc = open_pdf(make_sample_invoice(4))
        two = impose(doc, [0, 1], ImposeSettings(n_up=2, trim=True, cut_marks=True, scale="fit"))
        try:
            self.assertEqual(two.document.page_count, 1)
            self._assert_a4(two.document[0])
            self.assertGreaterEqual(len(two.document[0].get_xobjects()), 1)
            self.assertEqual(len(two.document[0].get_images()), 0)
            self.assertTrue(_has_axis_line(two.document[0], "horizontal"))
            self.assertIn("INVNO0001", two.document[0].get_text("text"))
            self.assertIn("INVNO0002", two.document[0].get_text("text"))
            margin = mm_to_pt(8)
            gutter = mm_to_pt(6)
            top, bottom = _slots(two.document[0].rect.width, two.document[0].rect.height, 2, margin, gutter)
            self.assertGreater(ink_span_ratio(two.document[0], top), 0.65)
            self.assertGreater(ink_span_ratio(two.document[0], bottom), 0.65)
        finally:
            two.document.close()

        plain = impose(doc, [0, 1], ImposeSettings(n_up=2, trim=False, cut_marks=False, scale="fit"))
        try:
            margin = mm_to_pt(8)
            gutter = mm_to_pt(6)
            top, _bottom = _slots(plain.document[0].rect.width, plain.document[0].rect.height, 2, margin, gutter)
            self.assertLess(ink_span_ratio(plain.document[0], top), 0.55)
            self.assertFalse(_has_axis_line(plain.document[0], "horizontal"))
        finally:
            plain.document.close()

        four = impose(doc, [0, 1, 2, 3], ImposeSettings(n_up=4, trim=True, cut_marks=True))
        try:
            self.assertEqual(four.document.page_count, 1)
            self._assert_a4(four.document[0])
            self.assertEqual(len(four.document[0].get_images()), 0)
            self.assertTrue(_has_axis_line(four.document[0], "horizontal"))
            self.assertTrue(_has_axis_line(four.document[0], "vertical"))
            text = four.document[0].get_text("text")
            for number in range(1, 5):
                self.assertIn(f"INVNO{number:04d}", text)
        finally:
            four.document.close()

        sheets = impose(doc, [0, 1, 2], ImposeSettings(n_up=2))
        try:
            self.assertEqual(sheets.document.page_count, 2)
        finally:
            sheets.document.close()
        doc.close()

    def test_actual_size_overflow_warns_and_still_places(self):
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(20, 20, 575, 800), color=(0, 0, 0), width=2)
        page.insert_text((40, 80), "BIGPAGE", fontsize=18, fontname="helv")
        result = impose(doc, [0], ImposeSettings(n_up=2, trim=True, scale="actual", cut_marks=False))
        try:
            self.assertTrue(result.warnings)
            self.assertIn("BIGPAGE", result.document[0].get_text("text"))
        finally:
            result.document.close()
        doc.close()

    def test_rotated_page_matches_onscreen_ink(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=600)
        page.draw_rect(pymupdf.Rect(60, 80, 240, 140), fill=(0, 0, 0))
        page.set_cropbox(pymupdf.Rect(40, 40, 320, 420))
        page.set_rotation(90)
        truth = _ink(page)
        result = impose(doc, [0], ImposeSettings(n_up=1, trim=False, scale="actual", cut_marks=False, margin_mm=0))
        try:
            placed = _ink(result.document[0])
            self.assertGreater(truth.height, truth.width * 2)
            self.assertGreater(placed.height, placed.width * 2)
            self.assertAlmostEqual(placed.width, truth.width, delta=6)
            self.assertAlmostEqual(placed.height, truth.height, delta=6)
        finally:
            result.document.close()
        doc.close()

    def test_text_replace_and_font_fallback(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 80), "发票号码", fontsize=16, fontname="china-s")
        page.insert_text((72, 120), "ABC", fontsize=16, fontname="helv")
        spans = list_spans(page)
        chinese = next(span for span in spans if "发票" in span["text"])
        latin = next(span for span in spans if span["text"] == "ABC")
        replaced = replace_span(page, chinese, "发票代码")
        self.assertFalse(replaced["font_replaced"])
        self.assertIn("发票代码", page.get_text("text"))
        self.assertNotIn("发票号码", page.get_text("text"))
        fallback = replace_span(page, latin, "中文")
        self.assertTrue(fallback["font_replaced"])
        self.assertIn("中文", page.get_text("text"))
        self.assertNotIn("ABC", page.get_text("text"))
        self.assertFalse(has_signature(doc))
        doc.close()

    def test_page_without_text_has_no_spans(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(40, 40, 120, 120), fill=(0, 0, 0))
        self.assertEqual(list_spans(page), [])
        doc.close()

    def test_print_path_paints_a4_pdf(self):
        doc = open_pdf(make_sample_invoice(2))
        imposed = impose(doc, [0, 1], ImposeSettings(n_up=2, trim=True))
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "printed.pdf"
            print_to_pdf(imposed.document, str(target))
            painted = open_pdf(target.read_bytes())
            self.assertEqual(painted.page_count, 1)
            self._assert_a4(painted[0])
            pixmap = painted[0].get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3), alpha=False)
            self.assertLess(min(pixmap.samples), 250)
            painted.close()
        imposed.document.close()
        doc.close()
        with self.assertRaises(RuntimeError):
            empty = pymupdf.open()
            empty.new_page()
            print_document(empty, "")
            empty.close()
        self.assertIsInstance(list_printers(), list)

    def test_window_loads_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = QSettings(str(Path(folder) / "settings.ini"), QSettings.Format.IniFormat)
            window = MainWindow(settings)
            window._load_sample()
            self.assertEqual(window.session.doc.page_count, 4)
            self.assertIsNotNone(window.preview._image)
            self.assertIn("INVNO0001", window.session.doc[0].get_text("text"))
            window.close()

    def test_folder_lists_pdfs_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "发票10.pdf").write_bytes(make_sample_invoice(1))
            (root / "发票2.pdf").write_bytes(make_sample_invoice(1))
            (root / "发票1.PDF").write_bytes(make_sample_invoice(1))
            (root / "说明.txt").write_bytes(b"no")
            nested = root / "子目录"
            nested.mkdir()
            (nested / "里面.pdf").write_bytes(make_sample_invoice(1))
            names = [Path(path).name for path in pdfs_in_folder(folder)]
            self.assertEqual(names, ["发票1.PDF", "发票2.pdf", "发票10.pdf"])

    def test_open_many_invoices_keeps_every_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "发票2.pdf"
            second = root / "发票10.pdf"
            first.write_bytes(_labeled_page("TWO"))
            second.write_bytes(_labeled_page("TEN"))
            settings = QSettings(str(Path(folder) / "settings.ini"), QSettings.Format.IniFormat)
            window = MainWindow(settings)
            try:
                dialog, picked = window._pdf_dialog("打开 PDF")
                self.assertEqual(dialog.fileMode(), dialog.FileMode.ExistingFiles)
                dialog.show()
                QApplication.processEvents()
                tree = dialog.findChild(QTreeView, "folderTree")
                splitter = dialog.findChild(QSplitter, "splitter")
                self.assertIs(splitter.widget(0), tree)
                self.assertGreaterEqual(tree.minimumWidth(), 260)
                self.assertGreaterEqual(tree.width(), 240)
                dialog.setDirectory(folder)
                QApplication.processEvents()
                self.assertEqual(Path(dialog.directory().absolutePath()).resolve(), Path(folder).resolve())
                button = next(item for item in dialog.findChildren(QPushButton) if "文件夹" in item.text())
                button.click()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual([Path(path).name for path in picked], ["发票2.pdf", "发票10.pdf"])
                dialog.close()

                window._open_paths(picked)
                self.assertEqual(window.session.doc.page_count, 2)
                self.assertEqual(window.pages.count(), 2)
                self.assertIn("TWO", window.session.doc[0].get_text("text"))
                self.assertIn("TEN", window.session.doc[1].get_text("text"))
                self.assertTrue(window.session.dirty)
                extra = root / "再打开.pdf"
                extra.write_bytes(_labeled_page("EXTRA"))
                again, _again_picked = window._pdf_dialog("打开 PDF")
                accept = next(item for item in again.findChildren(QPushButton) if "追加" in item.text())
                self.assertIn("追加", accept.text())
                again.close()
                window._open_paths([str(extra)])
                self.assertEqual(window.session.doc.page_count, 3)
                self.assertIn("TWO", window.session.doc[0].get_text("text"))
                self.assertIn("EXTRA", window.session.doc[2].get_text("text"))
                self.assertIn("已追加", window.statusBar().currentMessage())
                with self.assertRaises(PdfError):
                    window.session.save_to(str(first))
                with self.assertRaises(PdfError):
                    window.session.save_to(str(second))

                window.session.merge_many([(_labeled_page("MORE"), "", str(root / "再一张.pdf"))])
                self.assertEqual(window.session.doc.page_count, 4)
                self.assertIn("MORE", window.session.doc[3].get_text("text"))
            finally:
                window.session.dirty = False
                window.close()

    def test_failed_batch_leaves_the_open_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "发票.pdf"
            source.write_bytes(_labeled_page("KEEP"))
            session = PdfSession()
            session.load_bytes(source.read_bytes(), str(source))
            with self.assertRaises(PdfError):
                session.load_many(
                    [
                        (_labeled_page("NEW"), str(root / "新的.pdf"), ""),
                        (b"not a pdf", str(root / "坏.pdf"), ""),
                    ]
                )
            self.assertEqual(session.doc.page_count, 1)
            self.assertIn("KEEP", session.doc[0].get_text("text"))
            with self.assertRaises(PdfError):
                session.merge_many(
                    [
                        (_labeled_page("EXTRA"), "", str(root / "追加.pdf")),
                        (b"not a pdf", "", str(root / "坏.pdf")),
                    ]
                )
            self.assertEqual(session.doc.page_count, 1)
            session.close()

    def test_preview_switches_sheet_to_landscape(self):
        doc = open_pdf(make_sample_invoice(2))
        def rebuild(orientation):
            return impose(
                doc,
                [0, 1],
                ImposeSettings(n_up=2, trim=True, cut_marks=True, orientation=orientation),
            )

        first = rebuild("portrait")
        dialog = ImposePreviewDialog(first.document, first.warnings, lambda _doc: None, lambda _doc: None, rebuild)
        try:
            self.assertLess(dialog.document[0].rect.width, dialog.document[0].rect.height)
            dialog.landscape_button.click()
            page = dialog.document[0]
            self.assertGreater(page.rect.width, page.rect.height)
            self.assertAlmostEqual(page.rect.width, 842, delta=6)
            self.assertAlmostEqual(page.rect.height, 595, delta=6)
            self.assertIn("INVNO0001", page.get_text("text"))
            self.assertIn("INVNO0002", page.get_text("text"))
            self.assertTrue(_has_axis_line(page, "vertical"))
            self.assertIn("横版", dialog.page_label.text())
            dialog.portrait_button.click()
            self.assertLess(dialog.document[0].rect.width, dialog.document[0].rect.height)
            self.assertIn("竖版", dialog.page_label.text())
        finally:
            dialog.close()
            doc.close()

    def test_landscape_sheet_prints_wide(self):
        doc = open_pdf(make_sample_invoice(2))
        imposed = impose(doc, [0, 1], ImposeSettings(n_up=2, trim=True, orientation="landscape"))
        try:
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "wide.pdf"
                print_to_pdf(imposed.document, str(target))
                painted = open_pdf(target.read_bytes())
                try:
                    self.assertGreater(painted[0].rect.width, painted[0].rect.height)
                    self.assertAlmostEqual(painted[0].rect.width, 842, delta=8)
                    self.assertAlmostEqual(painted[0].rect.height, 595, delta=8)
                finally:
                    painted.close()
        finally:
            imposed.document.close()
            doc.close()

    def test_invoice_print_skips_duplicates_and_sums_once(self):
        sample = make_sample_invoice(1)
        rich = _digital_invoice()
        try:
            other = rich.tobytes()
        finally:
            rich.close()
        accepted, warnings = select_new_invoices(
            [(sample, "a.pdf", ""), (sample, "a-copy.pdf", ""), (other, "b.pdf", "")],
            set(),
            set(),
        )
        self.assertEqual([path for _data, path, _password in accepted], ["a.pdf", "b.pdf"])
        self.assertTrue(any("已经导入过" in line for line in warnings))

        changed = open_pdf(sample)
        changed.new_page()
        variant = changed.tobytes()
        changed.close()
        rejected, rejected_warnings = select_new_invoices([(variant, "variant.pdf", "")], {"INVNO0001"}, set())
        self.assertEqual(rejected, [])
        self.assertTrue(any("发票号码重复" in line for line in rejected_warnings))

        session = PdfSession()
        session.load_many([(sample, "plain.pdf", "")], role="document")
        session.merge_many([(other, "", "b.pdf"), (sample, "", "inv.pdf")], role="invoice")
        self.assertEqual(session.page_roles[0], "document")
        self.assertEqual(session.page_roles[1:], ["invoice", "invoice"])
        invoice_indexes = [index for index, role in enumerate(session.page_roles) if role == "invoice"]
        kept, skipped = dedupe_invoice_rows(extract_invoice_rows(session.doc, invoice_indexes))
        self.assertEqual(skipped, [])
        self.assertEqual(total_amount(kept), (2060.0, 0))
        self.assertEqual(
            select_new_invoices([(sample, "again.pdf", "")], {row.number for row in kept}, {file_id for file_id in session.page_file_ids if file_id})[0],
            [],
        )
        session.delete([0])
        self.assertEqual(session.page_roles, ["invoice", "invoice"])
        session.clear()
        self.assertIsNone(session.doc)
        self.assertEqual(session.page_roles, [])

        duplicated = kept + [kept[0]]
        unique, skipped = dedupe_invoice_rows(duplicated)
        self.assertEqual(len(unique), len(kept))
        self.assertEqual(len(skipped), 1)
        self.assertEqual(total_amount(unique)[0], 2060.0)

    def test_menu_follows_what_was_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = QSettings(str(Path(folder) / "settings.ini"), QSettings.Format.IniFormat)
            window = MainWindow(settings)
            try:
                self.assertTrue(window._menu_actions["打开"].isEnabled())
                self.assertTrue(window._menu_actions["发票打印"].isEnabled())
                self.assertFalse(window._menu_actions["另存为"].isEnabled())
                self.assertFalse(window._menu_actions["导出所选页"].isEnabled())
                self.assertFalse(window._menu_actions["按页拆分"].isEnabled())
                self.assertFalse(window._menu_actions["发票汇总"].isEnabled())
                self.assertFalse(window._menu_actions["清空"].isEnabled())

                window.session.load_many([(make_sample_invoice(1), str(Path(folder) / "发票.pdf"), "")], role="invoice")
                window._after_load()
                self.assertFalse(window._menu_actions["打开"].isEnabled())
                self.assertFalse(window._menu_actions["另存为"].isEnabled())
                self.assertFalse(window._menu_actions["导出所选页"].isEnabled())
                self.assertFalse(window._menu_actions["按页拆分"].isEnabled())
                self.assertTrue(window._menu_actions["发票打印"].isEnabled())
                self.assertTrue(window._menu_actions["发票汇总"].isEnabled())
                self.assertTrue(window._menu_actions["清空"].isEnabled())

                window.session.clear()
                window._clear_view()
                window.session.load_bytes(make_sample_invoice(1), str(Path(folder) / "普通.pdf"))
                window._after_load()
                self.assertTrue(window._menu_actions["打开"].isEnabled())
                self.assertTrue(window._menu_actions["另存为"].isEnabled())
                self.assertTrue(window._menu_actions["导出所选页"].isEnabled())
                self.assertTrue(window._menu_actions["按页拆分"].isEnabled())
                self.assertFalse(window._menu_actions["发票打印"].isEnabled())
                self.assertFalse(window._menu_actions["发票汇总"].isEnabled())
            finally:
                window.session.dirty = False
                window.close()

    def test_output_directory_is_created_on_desktop_root(self):
        with tempfile.TemporaryDirectory() as folder:
            created = output_directory(Path(folder))
            self.assertEqual(created, Path(folder) / "PDF输出")
            self.assertTrue(created.is_dir())

    def test_invoice_summary_rows_and_workbook(self):
        doc = open_pdf(make_sample_invoice(2))
        try:
            rows = extract_invoice_rows(doc)
        finally:
            doc.close()
        self.assertEqual([row.number for row in rows], ["INVNO0001", "INVNO0002"])
        self.assertEqual(rows[0].date, "2026年10月09日")
        self.assertEqual(rows[0].buyer, "示例科技有限公司")
        self.assertEqual(rows[0].seller, "")
        self.assertEqual(rows[0].total, "1000.00")
        self.assertEqual(rows[0].note, "")

        rich = _digital_invoice()
        try:
            row = extract_invoice_rows(rich)[0]
        finally:
            rich.close()
        self.assertEqual(row.number, "26112000000012345678")
        self.assertEqual(row.date, "2026年03月15日")
        self.assertEqual(row.buyer, "北京示例科技有限公司")
        self.assertEqual(row.seller, "上海示例贸易有限公司")
        self.assertEqual(row.amount, "1000.00")
        self.assertEqual(row.tax, "60.00")
        self.assertEqual(row.total, "1060.00")
        self.assertEqual(row.note, "")

        blank = pymupdf.open()
        blank.new_page()
        try:
            self.assertIn("没有文字层", extract_invoice_rows(blank)[0].note)
        finally:
            blank.close()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "汇总.xlsx"
            write_invoice_xlsx(str(path), rows)
            with zipfile.ZipFile(path) as package:
                sheet = package.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("INVNO0001", sheet)
        self.assertIn("示例科技有限公司", sheet)
        self.assertIn(">1000.00<", sheet)

        dialog = InvoiceTableDialog(rows, "发票汇总.xlsx")
        try:
            self.assertEqual(dialog.table.item(0, 1).text(), "INVNO0001")
            self.assertEqual(dialog.table.item(0, 3).text(), "示例科技有限公司")
            self.assertEqual(dialog.table.item(1, 1).text(), "INVNO0002")
            self.assertEqual(dialog.table.item(0, 7).text(), "1000.00")
        finally:
            dialog.close()

    def _assert_a4(self, page: pymupdf.Page) -> None:
        self.assertAlmostEqual(page.rect.width, 595, delta=6)
        self.assertAlmostEqual(page.rect.height, 842, delta=6)


if __name__ == "__main__":
    unittest.main()

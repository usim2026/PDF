"""Send an imposed PDF to a Windows printer at A4, without an extra fit-to-page shrink."""

from __future__ import annotations

import pymupdf
from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QImage, QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo


def list_printers() -> list[str]:
    return [printer.printerName() for printer in QPrinterInfo.availablePrinters() if printer.printerName()]


def default_printer_name() -> str:
    printer = QPrinterInfo.defaultPrinter()
    name = printer.printerName() if printer is not None else ""
    return name or ""


def print_document(doc: pymupdf.Document, printer_name: str, copies: int = 1) -> None:
    if not printer_name:
        raise RuntimeError("请先选择打印机")
    printer = _configure_printer(copies)
    printer.setPrinterName(printer_name)
    printer.setOutputFormat(QPrinter.OutputFormat.NativeFormat)
    printer.setDocName("发票拼版")
    _paint(doc, printer)


def print_to_pdf(doc: pymupdf.Document, path: str, copies: int = 1) -> None:
    """Same painting path as a printer, written to a PDF file for checks."""
    printer = _configure_printer(copies)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    _paint(doc, printer)


def _configure_printer(copies: int) -> QPrinter:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setResolution(300)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    printer.setPageOrientation(QPageLayout.Orientation.Portrait)
    printer.setColorMode(QPrinter.ColorMode.Color)
    printer.setFullPage(True)
    printer.setCopyCount(max(1, int(copies)))
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Portrait,
        QMarginsF(0, 0, 0, 0),
        QPageLayout.Unit.Millimeter,
    )
    printer.setPageLayout(layout)
    return printer


def _paint(doc: pymupdf.Document, printer: QPrinter) -> None:
    if doc.page_count < 1:
        raise RuntimeError("没有要打印的页")
    _apply_page_orientation(printer, doc[0])
    painter = QPainter()
    if not painter.begin(printer):
        message = printer.errorString() or "无法开始打印"
        raise RuntimeError(message)
    try:
        for index, page in enumerate(doc):
            if index:
                _apply_page_orientation(printer, page)
                if not printer.newPage():
                    raise RuntimeError("无法换页")
            target = printer.pageLayout().fullRectPixels(printer.resolution())
            image = _page_image(page, printer.resolution())
            painter.drawImage(target, image)
    finally:
        painter.end()


def _apply_page_orientation(printer: QPrinter, page: pymupdf.Page) -> None:
    orientation = (
        QPageLayout.Orientation.Landscape
        if page.rect.width > page.rect.height + 1
        else QPageLayout.Orientation.Portrait
    )
    printer.setPageOrientation(orientation)


def _page_image(page: pymupdf.Page, dpi: int) -> QImage:
    zoom = max(72, min(int(dpi), 300)) / 72.0
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    image = QImage(pixmap.samples, pixmap.width, pixmap.height, pixmap.stride, QImage.Format.Format_RGB888)
    return image.copy()

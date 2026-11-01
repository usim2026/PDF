"""A synthetic invoice used to try 2-up and 4-up without a real file."""

from __future__ import annotations

import pymupdf


def make_sample_invoice(pages: int = 4) -> bytes:
    """A4 pages whose invoice block sits in the upper half, like many e-invoices."""
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=595, height=842)
        box = pymupdf.Rect(36, 32, 559, 348)
        page.draw_rect(box, color=(0.15, 0.15, 0.15), width=1.2)
        page.insert_text((48, 64), "电子发票（示例）", fontsize=18, fontname="china-s", color=(0, 0, 0))
        page.insert_text(
            (48, 96),
            f"发票号码  INVNO{number + 1:04d}",
            fontsize=12,
            fontname="china-s",
        )
        page.insert_text((48, 120), "开票日期  2026年10月09日", fontsize=12, fontname="china-s")
        page.insert_text((48, 156), "购买方    示例科技有限公司", fontsize=12, fontname="china-s")
        page.draw_line(pymupdf.Point(48, 176), pymupdf.Point(547, 176), color=(0.2, 0.2, 0.2), width=0.8)
        page.insert_text((48, 202), "项目名称            金额", fontsize=12, fontname="china-s")
        page.insert_text((48, 230), "技术服务            1000.00", fontsize=12, fontname="china-s")
        page.insert_text((48, 270), "价税合计（小写）    ¥1000.00", fontsize=13, fontname="china-s")
        page.insert_text((48, 320), "开票人  张三", fontsize=11, fontname="china-s")
        _draw_qr(page, 468, 48)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def _draw_qr(page: pymupdf.Page, left: float, top: float) -> None:
    cell = 6
    for row in range(12):
        for col in range(12):
            if (row * 3 + col * 5 + row * col) % 3 == 0 or row in (0, 11) or col in (0, 11):
                x = left + col * cell
                y = top + row * cell
                page.draw_rect(pymupdf.Rect(x, y, x + cell - 1, y + cell - 1), fill=(0, 0, 0), color=(0, 0, 0))

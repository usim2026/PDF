"""Pull one bookkeeping row from each invoice page that has a text layer."""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf

from app.pdf.ops import open_pdf

_NUMBER = re.compile(r"发票号码\s*[:：]?\s*([A-Za-z0-9]{4,30})")
_DATE = re.compile(
    r"开票日期\s*[:：]?\s*"
    r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})"
)
_DATE_ONLY = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}")
_MONEY = re.compile(r"[0-9][0-9,]*\.\d{2}")
_YEN = re.compile(r"[¥￥]\s*([0-9][0-9,]*\.\d{2})")
_NAME_INLINE = re.compile(r"名称\s*[:：]\s*(.+)")
_NAME_ONLY = re.compile(r"名称\s*[:：]?$")

FIELDS = (
    ("page", "页码"),
    ("number", "发票号码"),
    ("date", "开票日期"),
    ("buyer", "购买方"),
    ("seller", "销售方"),
    ("amount", "金额"),
    ("tax", "税额"),
    ("total", "价税合计"),
    ("note", "备注"),
)


@dataclass
class InvoiceRow:
    page: int
    number: str = ""
    date: str = ""
    buyer: str = ""
    seller: str = ""
    amount: str = ""
    tax: str = ""
    total: str = ""
    note: str = ""

    def value(self, key: str) -> str:
        if key == "page":
            return str(self.page)
        return str(getattr(self, key))


def extract_invoice_rows(doc: pymupdf.Document, indices: list[int] | None = None) -> list[InvoiceRow]:
    chosen = list(range(doc.page_count)) if indices is None else indices
    rows: list[InvoiceRow] = []
    for index in chosen:
        if index < 0 or index >= doc.page_count:
            continue
        try:
            rows.append(_extract_page(doc[index], index + 1))
        except Exception:
            rows.append(InvoiceRow(page=index + 1, note="这一页读不出来"))
    return rows


def numbers_in_pdf(data: bytes, password: str = "") -> list[str]:
    doc = open_pdf(data, password)
    try:
        numbers = []
        for page in doc:
            number = _extract_page(page, 1).number.strip()
            if number:
                numbers.append(number)
        return numbers
    finally:
        doc.close()


def select_new_invoices(
    items: list[tuple[bytes, str | None, str]],
    existing_numbers: set[str],
    existing_file_ids: set[str],
) -> tuple[list[tuple[bytes, str | None, str]], list[str]]:
    """Skip files already imported or whose invoice number is already present."""
    accepted: list[tuple[bytes, str | None, str]] = []
    warnings: list[str] = []
    seen_numbers = set(existing_numbers)
    seen_files = set(existing_file_ids)
    batch_numbers: set[str] = set()
    for data, path, password in items:
        name = Path(path).name if path else "未命名"
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_files:
            warnings.append(f"{name} 已经导入过")
            continue
        numbers = numbers_in_pdf(data, password)
        repeated = [number for number in dict.fromkeys(numbers) if number in seen_numbers or number in batch_numbers]
        if repeated:
            warnings.append(f"{name} 发票号码重复：{'、'.join(repeated)}")
            continue
        accepted.append((data, path, password))
        seen_files.add(digest)
        batch_numbers.update(numbers)
    return accepted, warnings


def dedupe_invoice_rows(rows: list[InvoiceRow]) -> tuple[list[InvoiceRow], list[str]]:
    seen: dict[str, int] = {}
    kept: list[InvoiceRow] = []
    skipped: list[str] = []
    for row in rows:
        number = row.number.strip()
        if number and number in seen:
            skipped.append(f"第 {row.page} 页发票号码 {number} 与第 {seen[number]} 页重复")
            continue
        if number:
            seen[number] = row.page
        kept.append(row)
    return kept, skipped


def total_amount(rows: list[InvoiceRow]) -> tuple[float, int]:
    """Sum 价税合计, or 金额 when the total is missing. Returns (yuan, missing count)."""
    total = 0.0
    missing = 0
    for row in rows:
        money = _row_money(row)
        if money is None:
            missing += 1
        else:
            total += money
    return round(total, 2), missing


def format_yuan(amount: float) -> str:
    return f"{amount:,.2f} 元"


def _row_money(row: InvoiceRow) -> float | None:
    raw = (row.total or row.amount).replace(",", "").replace("，", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def write_invoice_xlsx(path: str, rows: list[InvoiceRow]) -> None:
    target = Path(path)
    sheet = _worksheet(rows)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _content_types())
        package.writestr("_rels/.rels", _root_rels())
        package.writestr("xl/workbook.xml", _workbook())
        package.writestr("xl/_rels/workbook.xml.rels", _workbook_rels())
        package.writestr("xl/worksheets/sheet1.xml", sheet)


def _extract_page(page: pymupdf.Page, page_number: int) -> InvoiceRow:
    cells = _cells(page)
    if not cells:
        return InvoiceRow(page=page_number, note="没有文字层，抽不出内容")
    lines = _cluster(cells)
    text = "\n".join(_line_text(line) for line in lines)
    row = InvoiceRow(
        page=page_number,
        number=_number(lines, text),
        date=_date(lines, text),
        amount="",
        tax="",
        total=_total(lines),
    )
    row.amount, row.tax = _totals(lines)
    row.buyer, row.seller = _parties(lines)
    if not any((row.number, row.date, row.buyer, row.seller, row.total)):
        row.note = "未识别为发票"
    return row


def _cells(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    found = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                found.append((float(x0), float(y0), float(x1), float(y1), text))
    return found


def _cluster(cells: list[tuple[float, float, float, float, str]]) -> list[list[tuple[float, float, float, float, str]]]:
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for cell in sorted(cells, key=lambda item: (item[1], item[0])):
        if rows and abs(cell[1] - rows[-1][0][1]) <= 5:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    for row in rows:
        row.sort(key=lambda item: item[0])
    return rows


def _line_text(row: list[tuple[float, float, float, float, str]]) -> str:
    return " ".join(cell[4] for cell in row)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _number(lines: list[list[tuple[float, float, float, float, str]]], text: str) -> str:
    match = _NUMBER.search(text)
    if match:
        return match.group(1)
    return _next_line(lines, ("发票号码",), re.compile(r"[A-Za-z0-9]{4,30}"))


def _date(lines: list[list[tuple[float, float, float, float, str]]], text: str) -> str:
    match = _DATE.search(text)
    if match:
        return _compact(match.group(1))
    found = _next_line(lines, ("开票日期",), _DATE_ONLY)
    return _compact(found) if found else ""


def _next_line(
    lines: list[list[tuple[float, float, float, float, str]]],
    labels: tuple[str, ...],
    pattern: re.Pattern[str],
) -> str:
    for index, row in enumerate(lines):
        compact = _compact(_line_text(row))
        if compact not in labels and not any(compact == f"{label}：" or compact == f"{label}:" for label in labels):
            continue
        if index + 1 >= len(lines):
            return ""
        match = pattern.search(_line_text(lines[index + 1]))
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return ""


def _total(lines: list[list[tuple[float, float, float, float, str]]]) -> str:
    for row in lines:
        compact = _compact(_line_text(row))
        if "价税合计" not in compact and "小写" not in compact:
            continue
        amounts = _amounts(_line_text(row))
        if amounts:
            return amounts[-1]
    return ""


def _totals(lines: list[list[tuple[float, float, float, float, str]]]) -> tuple[str, str]:
    for row in lines:
        compact = _compact(_line_text(row))
        if "合计" not in compact or "价税合计" in compact:
            continue
        amounts = _amounts(_line_text(row))
        if len(amounts) >= 2:
            return amounts[-2], amounts[-1]
        if len(amounts) == 1:
            return amounts[0], ""
    return "", ""


def _amounts(text: str) -> list[str]:
    yen = [_plain_money(item) for item in _YEN.findall(text)]
    if yen:
        return yen
    return [_plain_money(item) for item in _MONEY.findall(text)]


def _plain_money(value: str) -> str:
    return value.replace(",", "").replace("，", "")


def _parties(lines: list[list[tuple[float, float, float, float, str]]]) -> tuple[str, str]:
    buyer_x, seller_x = _party_anchors(lines)
    buyer = ""
    seller = ""
    for row in lines:
        for origin, name in _names_in_row(row):
            if _side(origin, buyer_x, seller_x) == "seller":
                seller = seller or name
            else:
                buyer = buyer or name
    if not buyer:
        buyer = _party_label(lines, "购买方") or _party_label(lines, "购方")
    if not seller:
        seller = _party_label(lines, "销售方") or _party_label(lines, "销方")
    return buyer, seller


def _party_anchors(lines: list[list[tuple[float, float, float, float, str]]]) -> tuple[float | None, float | None]:
    buyer_x = None
    seller_x = None
    for row in lines:
        for x0, _y0, _x1, _y1, text in row:
            compact = _compact(text)
            if buyer_x is None and (compact.startswith("购买方") or compact.startswith("购方")):
                buyer_x = x0
            if seller_x is None and (compact.startswith("销售方") or compact.startswith("销方")):
                seller_x = x0
    return buyer_x, seller_x


def _names_in_row(row: list[tuple[float, float, float, float, str]]) -> list[tuple[float, str]]:
    found = []
    for index, cell in enumerate(row):
        text = cell[4].strip()
        inline = _NAME_INLINE.match(text)
        if inline:
            name = _clean_name(inline.group(1))
            if name:
                found.append((cell[0], name))
            continue
        if _NAME_ONLY.match(text) and index + 1 < len(row):
            name = _clean_name(row[index + 1][4])
            if name:
                found.append((cell[0], name))
    return found


def _party_label(lines: list[list[tuple[float, float, float, float, str]]], label: str) -> str:
    pattern = re.compile(rf"{label}\s*[:：]?\s*(.*)")
    for row in lines:
        for index, cell in enumerate(row):
            match = pattern.match(cell[4].strip())
            if not match:
                continue
            rest = match.group(1).strip()
            if rest and not rest.startswith("信息"):
                name = _clean_name(rest)
                if name:
                    return name
            if index + 1 < len(row):
                name = _clean_name(row[index + 1][4])
                if name and not name.startswith(("信息", "名称", "统一", "纳税人")):
                    return name
    return ""


def _side(origin: float, buyer_x: float | None, seller_x: float | None) -> str:
    if seller_x is None:
        return "buyer"
    if buyer_x is None:
        return "seller"
    if seller_x >= buyer_x:
        return "seller" if origin >= (buyer_x + seller_x) / 2 else "buyer"
    return "buyer" if origin >= (buyer_x + seller_x) / 2 else "seller"


def _clean_name(value: str) -> str:
    value = re.split(r"统一社会信用代码|纳税人识别号|地址|电话|开户行", value, maxsplit=1)[0]
    value = value.strip(" ：:;；")
    if value in {"", "信息", "名称", "购买方", "销售方", "购方", "销方", "购买方信息", "销售方信息"}:
        return ""
    if re.fullmatch(r"[0-9A-Z]{15,20}", value):
        return ""
    return value


def _worksheet(rows: list[InvoiceRow]) -> str:
    body = [_row_xml(1, [title for _key, title in FIELDS], numeric=set())]
    numeric = {"page", "amount", "tax", "total"}
    for offset, row in enumerate(rows, start=2):
        body.append(_row_xml(offset, [row.value(key) for key, _title in FIELDS], numeric={index for index, (key, _title) in enumerate(FIELDS) if key in numeric}))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def _row_xml(index: int, values: list[str], numeric: set[int]) -> str:
    cells = []
    for column, value in enumerate(values):
        if value == "":
            continue
        ref = f"{_column_name(column)}{index}"
        if column in numeric and _is_number(value):
            cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        else:
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>')
    return f'<row r="{index}">{"".join(cells)}</row>'


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value))


def _column_name(index: int) -> str:
    name = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="发票汇总" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )


def _workbook_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

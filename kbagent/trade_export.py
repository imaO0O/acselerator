"""Draft trade proposals. No network access, spreadsheet formulas or global tax rate.

``calculate`` returns Decimal amounts (or None for unknowns), never floats.
Rows retain input metadata and add offered, unit_price, vat_rate, line_total
(in the input price basis), subtotal (net), vat and total (gross).
Only exact/analog rows contribute to totals; callers must approve analogs in
the UI before assigning that status. Exporting itself never approves a draft.
"""
import io
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


CENT = Decimal("0.01")
OFFERED = frozenset({"exact", "analog"})
STATUSES = OFFERED | {"review", "out_of_stock", "not_found"}
STATUS_LABELS = {
    "exact": "Точное совпадение", "analog": "Аналог подтверждён",
    "review": "Требует проверки", "out_of_stock": "Недостаточно на складе",
    "not_found": "Не найдено",
}
UNKNOWN = "Неизвестно"
DRAFT = "ЧЕРНОВИК — требуется проверка и утверждение сотрудником"
FIELDS = (
    ("line", "№"), ("query", "Запрос"), ("status", "Статус"),
    ("qty", "Количество"), ("article", "Артикул"), ("name", "Наименование"),
    ("brand", "Бренд"), ("unit_price", "Цена за единицу"),
    ("line_total", "Сумма в базе цены"), ("vat_rate", "НДС, %"),
    ("subtotal", "Без НДС"), ("vat", "НДС"), ("total", "С НДС"),
    ("stock", "Наличие"), ("lead_time", "Срок поставки"),
    ("source", "Источник"), ("source_row", "Строка источника"),
    ("offered", "Включено в предложение"),
)


def _decimal(value, field, *, optional=False):
    if value is None and optional:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite nonnegative number") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return result


def _money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate(positions: list[dict], prices_include_vat=True) -> dict:
    """Calculate per-line, ROUND_HALF_UP to cents, then sum rounded lines.

    Inclusive: round qty*price as gross, extract/round VAT, net=gross-VAT.
    Exclusive: round qty*price as net, round net*rate/100 as VAT.
    Missing rates leave VAT and the opposite price basis unknown, even when
    qty/price is zero. Missing prices leave all money unknown.

    Result: rows; subtotal/vat/total (None if any offered row lacks that
    amount); known_subtotal/known_vat/known_total (partial sums);
    offered_count/excluded_count/unknown_price_count/unknown_rate_count;
    totals_complete (all offered money known), complete (also no excluded
    rows), prices_include_vat, draft and requires_human_review.
    Empty offered sets have zero sums, but excluded rows keep complete false.
    """
    if not isinstance(prices_include_vat, bool):
        raise ValueError("prices_include_vat must be a bool")
    rows = []
    # Isolate rounding/precision from callers' Decimal context.
    with localcontext() as context:
        context.prec = 80
        for position in positions:
            status = position["status"]
            if status not in STATUSES:
                raise ValueError(f"Unknown position status: {status}")
            selected = position.get("selected") or {}
            qty = _decimal(position["qty"], "qty")
            price = _decimal(selected.get("price"), "price", optional=True)
            rate = _decimal(selected.get("vat_rate"), "vat_rate", optional=True)
            row = {key: selected.get(key) for key in (
                "article", "name", "brand", "stock", "lead_time", "source", "source_row"
            )}
            row.update(line=position["line"], query=position["query"], status=status,
                       qty=qty, offered=status in OFFERED, unit_price=price,
                       vat_rate=rate, line_total=None, subtotal=None, vat=None, total=None)
            if price is not None:
                amount = _money(qty * price)
                row["line_total"] = amount
                row["total" if prices_include_vat else "subtotal"] = amount
                if rate is not None:
                    vat = _money(amount * rate / (100 + rate) if prices_include_vat
                                 else amount * rate / 100)
                    row["vat"] = vat
                    row["subtotal" if prices_include_vat else "total"] = (
                        amount - vat if prices_include_vat else amount + vat
                    )
            rows.append(row)
        offered = [row for row in rows if row["offered"]]
        result = {"rows": rows, "prices_include_vat": prices_include_vat,
                  "offered_count": len(offered), "excluded_count": len(rows) - len(offered),
                  "unknown_price_count": sum(r["unit_price"] is None for r in offered),
                  "unknown_rate_count": sum(r["vat_rate"] is None for r in offered),
                  "draft": True, "requires_human_review": True}
        for field in ("subtotal", "vat", "total"):
            known = sum((r[field] for r in offered if r[field] is not None), Decimal("0.00"))
            result[f"known_{field}"] = known
            result[field] = known if all(r[field] is not None for r in offered) else None
        result["totals_complete"] = all(result[f] is not None for f in ("subtotal", "vat", "total"))
        result["complete"] = result["totals_complete"] and not result["excluded_count"]
    return result


def _text(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value)


def _notes(result):
    return [
        "Цены включают НДС." if result["prices_include_vat"] else "Цены указаны без НДС.",
        "В итогах только точные совпадения и подтверждённые аналоги. Остальные строки исключены.",
        f"Предлагается строк: {result['offered_count']}; исключено: {result['excluded_count']}.",
        f"Неизвестна цена: {result['unknown_price_count']}; ставка НДС: {result['unknown_rate_count']} "
        "(среди предлагаемых строк).",
        "Расчёт полный; требуется проверка сотрудником." if result["complete"] else
        "Предложение неполное: есть исключённые строки или неизвестные суммы. "
        "Известные части не являются полным итогом предложения.",
    ]


def _summaries(result):
    for field, label in (("subtotal", "Без НДС"), ("vat", "НДС"), ("total", "С НДС")):
        yield f"Итого предлагаемых строк — {label}", result[field]
        if result[field] is None:
            yield f"Известная часть — {label} (не полный итог)", result[f"known_{field}"]


def _literal(cell, value):
    """Force all untrusted strings to inline text, including =, +, -, @ and URLs."""
    if value is None or isinstance(value, (str, bool)):
        cell.value = _text(value)
        cell.data_type = "s"
        cell.quotePrefix = True
    else:
        cell.value = value
    cell.hyperlink = None


def export_xlsx(positions: list[dict], *, customer='', prices_include_vat=True) -> bytes:
    """Export all positions and strict/partial offered totals as a static XLSX."""
    result = calculate(positions, prices_include_vat)
    book = Workbook()
    sheet = book.active
    sheet.title = "Предложение"
    widths = {"line": 7, "query": 48, "status": 27, "qty": 14, "article": 22,
              "name": 48, "brand": 22, "source": 42, "source_row": 18,
              "offered": 24}
    for column, (key, _) in enumerate(FIELDS, 1):
        sheet.column_dimensions[get_column_letter(column)].width = widths.get(key, 22)
    for row in [[DRAFT], ["Заказчик", customer], *[[n] for n in _notes(result)]]:
        sheet.append([None] * len(row))
        for cell, value in zip(sheet[sheet.max_row], row):
            _literal(cell, value)
        if len(row) == 1:
            sheet.merge_cells(start_row=sheet.max_row, start_column=1,
                              end_row=sheet.max_row, end_column=len(FIELDS))
        else:
            sheet.merge_cells(start_row=sheet.max_row, start_column=2,
                              end_row=sheet.max_row, end_column=len(FIELDS))
        sheet.cell(sheet.max_row, 1).alignment = Alignment(wrap_text=True, vertical="top")
        sheet.row_dimensions[sheet.max_row].height = 30
    header_row = sheet.max_row + 2
    for column, (_, label) in enumerate(FIELDS, 1):
        cell = sheet.cell(header_row, column)
        _literal(cell, label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="334155")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[header_row].height = 32
    for number, row in enumerate(result["rows"], header_row + 1):
        lines = 1
        for column, (key, _) in enumerate(FIELDS, 1):
            cell = sheet.cell(number, column)
            value = STATUS_LABELS[row[key]] if key == "status" else row[key]
            _literal(cell, value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            width = widths.get(key, 22) - 2
            lines = max(lines, sum(max(1, math.ceil(len(part) / width))
                                   for part in _text(value).split("\n")))
            if key in {"unit_price", "line_total", "subtotal", "vat", "total"}:
                cell.number_format = "0.00"
        sheet.row_dimensions[number].height = min(409, max(30, 16 * lines + 6))
    end_row = header_row + len(result["rows"])
    sheet.auto_filter.ref = f"A{header_row}:R{end_row}"
    sheet.freeze_panes = f"E{header_row + 1}"
    for number, (label, value) in enumerate(_summaries(result), end_row + 2):
        _literal(sheet.cell(number, 1), label)
        sheet.merge_cells(start_row=number, start_column=1, end_row=number, end_column=7)
        sheet.cell(number, 1).alignment = Alignment(wrap_text=True, vertical="top")
        _literal(sheet.cell(number, 8), value)
        sheet.cell(number, 8).number_format = "0.00"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = f"{header_row}:{header_row}"
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def proposal_docx(positions: list[dict], *, customer='', prices_include_vat=True) -> bytes:
    """Export a draft proposal with all rows, quantities, provenance and unknowns."""
    result = calculate(positions, prices_include_vat)
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Mm(297), Mm(210)
    section.left_margin = section.right_margin = Mm(15)
    section.top_margin = section.bottom_margin = Mm(12)
    doc.styles["Normal"].font.size = Pt(10)
    doc.styles["Normal"].paragraph_format.space_after = Pt(4)
    for name in ("Title", "Heading 1", "Heading 2"):
        doc.styles[name].font.color.rgb = RGBColor(0, 0, 0)
    doc.add_heading("Коммерческое предложение", 0)
    doc.add_paragraph(DRAFT)
    doc.add_paragraph(f"Заказчик: {customer}")
    for note in _notes(result):
        doc.add_paragraph(note)
    basis = "с НДС" if prices_include_vat else "без НДС"
    headers = ["№", "Артикул / наименование", "Кол-во", f"Цена за ед.\n{basis}",
               f"Сумма\n{basis}", "НДС\nставка / сумма", "Наличие", "Срок поставки", "Статус"]
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False
    widths_mm = [8, 77, 18, 25, 27, 31, 22, 25, 34]
    for column, width in zip(table.columns, widths_mm):
        column.width = Mm(width)
    # Repeat column labels on continuation pages; let long body rows expand.
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{edge}")
        for key, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
            border.set(qn(f"w:{key}"), value)
        borders.append(border)
    table._tbl.tblPr.append(borders)
    sources = {}
    records = [headers]
    for row in result["rows"]:
        source = (_text(row["source"]), _text(row["source_row"]))
        if source not in sources:
            sources[source] = len(sources) + 1
        description = (f"{_text(row['article'])} — {_text(row['name'])}\n"
                       f"Запрос: {row['query']}\nБренд: {_text(row['brand'])}; [{sources[source]}]")
        vat = f"{_text(row['vat_rate'])}{' %' if row['vat_rate'] is not None else ''}\n{_text(row['vat'])}"
        status = STATUS_LABELS[row["status"]] + ("\nВключено" if row["offered"] else "\nИсключено")
        records.append([row["line"], description, row["qty"], row["unit_price"],
                        row["line_total"], vat, row["stock"], row["lead_time"], status])
    for index, values in enumerate(records):
        cells = table.rows[0].cells if index == 0 else table.add_row().cells
        for column, (cell, value, width) in enumerate(zip(cells, values, widths_mm)):
            cell.width = Mm(width)
            cell.text = _text(value)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if index == 0 or index % 2 == 0:
                shading = OxmlElement("w:shd")
                shading.set(qn("w:fill"), "E2E8F0" if index == 0 else "F8FAFC")
                cell._tc.get_or_add_tcPr().append(shading)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(3)
                paragraph.paragraph_format.space_before = Pt(3)
                paragraph.paragraph_format.keep_with_next = False
                paragraph.alignment = (WD_ALIGN_PARAGRAPH.LEFT if column == 1 else
                                       WD_ALIGN_PARAGRAPH.CENTER)
                for run in paragraph.runs:
                    run.font.size = Pt(9)
                    run.bold = index == 0
    doc.add_heading("Итоги предлагаемых строк", level=1)
    for label, value in _summaries(result):
        doc.add_paragraph(f"{label}: {_text(value)}")
    if sources:
        doc.add_heading("Источники", level=1)
        for (source, source_row), reference in sources.items():
            doc.add_paragraph(f"[{reference}] {source}; строка источника: {source_row}")
    doc.add_paragraph(DRAFT)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()

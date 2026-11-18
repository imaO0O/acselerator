"""Draft trade proposals. No network access, spreadsheet formulas or global tax rate.

``calculate`` returns Decimal amounts (or None for unknowns), never floats.
Rows retain input metadata and add offered, unit_price, vat_rate, line_total
(in the input price basis), subtotal (net), vat and total (gross).
Only exact/analog rows contribute to totals; callers must approve analogs in
the UI before assigning that status. Exporting itself never approves a draft.
"""
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from docx import Document
from docx.shared import Mm, Pt
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


CENT = Decimal("0.01")
OFFERED = frozenset({"exact", "analog"})
STATUSES = OFFERED | {"review", "out_of_stock", "not_found"}
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
        "В итогах только exact/analog; аналоги требуют предварительного утверждения в интерфейсе.",
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
    for row in [[DRAFT], ["Заказчик", customer], *[[n] for n in _notes(result)]]:
        sheet.append([None] * len(row))
        for cell, value in zip(sheet[sheet.max_row], row):
            _literal(cell, value)
    header_row = sheet.max_row + 2
    for column, (_, label) in enumerate(FIELDS, 1):
        cell = sheet.cell(header_row, column)
        _literal(cell, label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="334155")
    for number, row in enumerate(result["rows"], header_row + 1):
        for column, (key, _) in enumerate(FIELDS, 1):
            cell = sheet.cell(number, column)
            _literal(cell, row[key])
            if key in {"unit_price", "line_total", "subtotal", "vat", "total"}:
                cell.number_format = "0.00"
    end_row = header_row + len(result["rows"])
    sheet.auto_filter.ref = f"A{header_row}:R{end_row}"
    sheet.freeze_panes = f"E{header_row + 1}"
    for number, (label, value) in enumerate(_summaries(result), end_row + 2):
        _literal(sheet.cell(number, 1), label)
        _literal(sheet.cell(number, 8), value)
        sheet.cell(number, 8).number_format = "0.00"
    for column, (key, _) in enumerate(FIELDS, 1):
        sheet.column_dimensions[get_column_letter(column)].width = (
            42 if key in {"query", "name", "source"} else 22
        )
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
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.left_margin = section.right_margin = Mm(15)
    doc.styles["Normal"].font.size = Pt(10)
    doc.add_heading("Торговое предложение", 0)
    doc.add_paragraph(DRAFT)
    doc.add_paragraph(f"Заказчик: {customer}")
    for note in _notes(result):
        doc.add_paragraph(note)
    # A compact table per position keeps long names and source paths readable.
    for row in result["rows"]:
        doc.add_heading(f"Позиция {row['line']}: {row['query']}", level=2)
        table = doc.add_table(rows=0, cols=2)
        table.style = "Table Grid"
        for key, label in FIELDS:
            cells = table.add_row().cells
            cells[0].text, cells[1].text = label, _text(row[key])
    doc.add_heading("Итоги предлагаемых строк", level=1)
    for label, value in _summaries(result):
        doc.add_paragraph(f"{label}: {_text(value)}")
    doc.add_paragraph(DRAFT)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()

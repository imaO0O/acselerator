"""Proposal arithmetic, incomplete data, static Office output and provenance."""
import copy
import io
from decimal import Decimal, localcontext
from zipfile import ZipFile

import pytest
from docx import Document
from openpyxl import load_workbook

from kbagent.trade_export import calculate, export_xlsx, proposal_docx


def position(line=1, *, status="exact", qty=2, price=120, rate=20):
    return {"line": line, "query": f"Запрос {line}", "qty": qty, "status": status,
            "selected": {"article": "00123", "name": "Изделие", "brand": "Бренд",
                         "price": price, "vat_rate": rate, "stock": 0,
                         "lead_time": "4 недели", "source": "https://vendor.example/price.xlsx",
                         "source_row": 17}}


@pytest.mark.parametrize("inclusive,expected", [
    (True, ("200.00", "40.00", "240.00")),
    (False, ("240.00", "48.00", "288.00")),
])
def test_price_basis_and_no_mutation(inclusive, expected):
    rows = [position()]
    before = copy.deepcopy(rows)
    result = calculate(rows, inclusive)
    assert tuple(result[f] for f in ("subtotal", "vat", "total")) == tuple(map(Decimal, expected))
    assert result["rows"][0]["line_total"] == Decimal("240.00")
    assert result["complete"] and result["totals_complete"]
    assert result["draft"] and result["requires_human_review"]
    assert rows == before


def test_mixed_item_rates_and_rounding_per_line():
    rows = [position(i, qty=1, price="0.025", rate=rate)
            for i, rate in enumerate([0, 10, 37.5], 1)]
    result = calculate(rows, False)
    assert [r["subtotal"] for r in result["rows"]] == [Decimal("0.03")] * 3
    assert result["subtotal"] == Decimal("0.09")
    assert result["vat"] == Decimal("0.01")
    assert result["total"] == Decimal("0.10")
    inclusive = calculate([position(qty=1, price="0.03", rate=20)])
    assert inclusive["vat"] == Decimal("0.01")
    assert inclusive["subtotal"] == Decimal("0.02")


def test_float_conversion_and_callers_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        result = calculate([position(qty=0.1, price=12345.67, rate=0)], False)
    assert result["total"] == Decimal("1234.57")


def test_only_confirmed_statuses_contribute():
    rows = [position(i, status=status) for i, status in enumerate(
        ["exact", "analog", "review", "out_of_stock", "not_found"], 1)]
    result = calculate(rows)
    assert len(result["rows"]) == 5
    assert result["offered_count"] == 2
    assert result["excluded_count"] == 3
    assert result["total"] == Decimal("480.00")
    assert result["totals_complete"] and not result["complete"]
    assert [r["offered"] for r in result["rows"]] == [True, True, False, False, False]


@pytest.mark.parametrize("inclusive,known_field,unknown_field", [
    (True, "total", "subtotal"), (False, "subtotal", "total"),
])
def test_unknown_rate_preserves_known_price_basis(inclusive, known_field, unknown_field):
    result = calculate([position(rate=None)], inclusive)
    assert result[known_field] == Decimal("240.00")
    assert result[unknown_field] is None and result["vat"] is None
    assert result["unknown_rate_count"] == 1
    assert result[f"known_{unknown_field}"] == Decimal("0.00")
    assert not result["complete"] and not result["totals_complete"]


def test_unknown_prices_and_missing_selection_are_not_zero():
    missing = position(3)
    missing["selected"] = None
    result = calculate([position(), position(2, price=None), missing])
    assert result["total"] is None
    assert result["known_total"] == Decimal("240.00")
    assert result["unknown_price_count"] == 2
    assert result["rows"][1]["line_total"] is None
    assert result["rows"][2]["vat_rate"] is None


def test_zero_is_known_but_does_not_imply_known_rate():
    assert calculate([position(price=0, rate=0)])["total"] == Decimal("0.00")
    result = calculate([position(qty=0, rate=None)])
    assert result["total"] == Decimal("0.00")
    assert result["vat"] is None


def test_empty_and_entirely_excluded_proposals():
    result = calculate([])
    assert result["total"] == Decimal("0.00")
    assert result["offered_count"] == 0
    result = calculate([position(status="review", price=None, rate=None)])
    assert result["total"] == Decimal("0.00")
    assert result["unknown_price_count"] == 0
    assert not result["complete"]


@pytest.mark.parametrize("field,value", [
    ("qty", -1), ("qty", float("nan")), ("price", float("inf")),
    ("price", -0.01), ("vat_rate", -1), ("vat_rate", "bogus"),
])
def test_invalid_numeric_values_fail_explicitly(field, value):
    row = position()
    (row if field == "qty" else row["selected"])[field] = value
    with pytest.raises(ValueError, match=field):
        calculate([row])


def test_invalid_status_and_basis_fail_explicitly():
    with pytest.raises(ValueError, match="status"):
        calculate([position(status="unapproved")])
    with pytest.raises(ValueError, match="bool"):
        calculate([], "false")


def sheet_values(payload):
    book = load_workbook(io.BytesIO(payload), data_only=False)
    sheet = book.active
    values = list(sheet.values)
    header_index = next(i for i, row in enumerate(values) if row[0] == "№")
    return book, sheet, values, header_index


def test_xlsx_preserves_all_rows_unknowns_provenance_and_totals():
    rows = [position(), position(2, status="analog", price=None, rate=None),
            position(3, status="out_of_stock")]
    book, sheet, values, start = sheet_values(export_xlsx(rows, customer="Покупатель"))
    headers = values[start]
    exported = [dict(zip(headers, row)) for row in values[start + 1:start + 4]]
    assert [r["Статус"] for r in exported] == [r["status"] for r in rows]
    assert exported[0]["Количество"] == 2
    assert exported[0]["Артикул"] == "00123"
    assert exported[0]["Источник"] == rows[0]["selected"]["source"]
    assert exported[0]["Строка источника"] == 17
    assert exported[0]["Наличие"] == 0
    assert exported[1]["Цена за единицу"] == "Неизвестно"
    assert exported[1]["НДС, %"] == "Неизвестно"
    assert exported[2]["Включено в предложение"] == "Нет"
    assert next(r[7] for r in values if r[0] == "Итого предлагаемых строк — С НДС") == "Неизвестно"
    assert next(r[7] for r in values if r[0] == "Известная часть — С НДС (не полный итог)") == 240
    assert any("ЧЕРНОВИК" in str(r[0]) for r in values)
    assert any("неполное" in str(r[0]) for r in values)
    assert sheet.freeze_panes and len(book.sheetnames) == 1


@pytest.mark.parametrize("payload", [
    '=HYPERLINK("https://evil.example","click")', '+cmd|\'/C calc\'!A0',
    "-1+1", "@SUM(A1)", "\t=1+1", "https://evil.example/price.xlsx",
    "'[external.xlsx]Sheet1'!A1",
])
def test_xlsx_untrusted_fields_are_literal_without_active_links(payload):
    row = position()
    row["query"] = payload
    for key in ("article", "name", "brand", "stock", "lead_time", "source", "source_row"):
        row["selected"][key] = payload
    data = export_xlsx([row], customer=payload)
    _, sheet, _, start = sheet_values(data)
    for column in (2, 5, 6, 7, 14, 15, 16, 17):
        cell = sheet.cell(start + 2, column)
        assert cell.value == payload
        assert cell.data_type == "s" and cell.quotePrefix
    assert sheet["B2"].value == payload and sheet["B2"].data_type == "s"
    assert all(c.data_type != "f" and c.hyperlink is None for r in sheet for c in r)
    with ZipFile(io.BytesIO(data)) as archive:
        assert not any("externalLinks" in name for name in archive.namelist())
        assert all(b'TargetMode="External"' not in archive.read(name)
                   for name in archive.namelist() if name.endswith(".rels"))


def test_docx_quantities_totals_all_rows_and_explicit_unknowns():
    rows = [position(qty=2.5), position(2, status="review"),
            position(3, status="analog", price=None, rate=None)]
    data = proposal_docx(rows, customer="Покупатель", prices_include_vat=False)
    doc = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "ЧЕРНОВИК" in text and "Покупатель" in text
    assert "неполное" in text and "не являются полным итогом" in text
    assert "Цены указаны без НДС" in text
    assert "Итого предлагаемых строк — С НДС: Неизвестно" in text
    assert "Известная часть — С НДС (не полный итог): 360.00" in text
    assert len(doc.tables) == 3
    first = {r.cells[0].text: r.cells[1].text for r in doc.tables[0].rows}
    assert first["Количество"] == "2.5"
    assert first["Без НДС"] == "300.00" and first["НДС"] == "60.00"
    assert first["С НДС"] == "360.00"
    assert first["Источник"] == rows[0]["selected"]["source"]
    assert first["Строка источника"] == "17"
    last = {r.cells[0].text: r.cells[1].text for r in doc.tables[2].rows}
    assert last["Цена за единицу"] == "Неизвестно"
    assert last["НДС"] == "Неизвестно"
    with ZipFile(io.BytesIO(data)) as archive:
        assert all(b'TargetMode="External"' not in archive.read(name)
                   for name in archive.namelist() if name.endswith(".rels"))


@pytest.mark.parametrize("exporter", [export_xlsx, proposal_docx])
def test_empty_export_is_valid_office_file(exporter):
    assert ZipFile(io.BytesIO(exporter([]))).testzip() is None

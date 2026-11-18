"""Bounded offline check: structured trade demo, matching and Office exports.

This covers these exercised trade paths with the two network entry points below
blocked. It makes no assertion about outbound access by the rest of RAG.
"""

from collections import Counter
import copy
import io
import json
import re
import socket
from zipfile import ZipFile

from docx import Document
from openpyxl import load_workbook
import requests


def test_structured_trade_demo_and_selected_exports_without_network(monkeypatch, tmp_path):
    attempts = []

    def blocked_socket(*args, **kwargs):
        attempts.append("socket.create_connection")
        raise AssertionError("Structured trade workflow attempted a socket connection")

    def blocked_request(*args, **kwargs):
        attempts.append("requests.Session.request")
        raise AssertionError("Structured trade workflow attempted an HTTP request")

    monkeypatch.setattr(socket, "create_connection", blocked_socket)
    monkeypatch.setattr(requests.Session, "request", blocked_request)

    # Keep the guards active during imports, PDF extraction, catalog operations,
    # both exporters and reopening their bytes with the actual Office libraries.
    from kbagent.trade_catalog import TradeCatalog
    from kbagent.trade_demo import CASES, CATALOG_MAPPING, DEMO_ROOT, evaluate
    from kbagent.trade_export import export_xlsx, proposal_docx
    from kbagent.trade_requests import match_batch, read_file

    report = evaluate()
    assert report["passed"], json.dumps(report, ensure_ascii=False, indent=2)
    totals = {
        "exact_accuracy": 5,
        "cross_candidate_coverage": 3,
        "description_candidate_coverage": 5,
        "drawing_text_article_accuracy": 1,
        "no_data_accuracy": 3,
    }
    assert report["metrics"] == {
        metric: {"correct": total, "total": total, "value": 1.0}
        for metric, total in totals.items()
    }

    selected_rows, expected_selections = [], []
    for case in CASES:
        folder = DEMO_ROOT / case
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        catalog = TradeCatalog(tmp_path / f"{case}.sqlite")
        imported = catalog.import_table("catalog.csv", (folder / "catalog.csv").read_bytes(),
                                        CATALOG_MAPPING, catalog=f"ДЕМО / {case}")
        assert imported["imported"] == expected["catalog_rows"] and imported["rejected"] == 0
        batches = [(read_file("request.csv", (folder / "request.csv").read_bytes())["items"],
                    expected["positions"])]
        if "drawing" in expected:
            drawing = read_file("drawing.pdf", (folder / "drawing.pdf").read_bytes())
            batches.append((drawing["items"], expected["drawing"]["positions"]))
            batches.append(([{"query": drawing["fields"]["designation"], "qty": 1}],
                            [{"kind": "exact", "articles": [expected["drawing"]["article"]], "qty": 1}]))
        for items, targets in batches:
            matched = match_batch(catalog, items)
            selected_rows.extend(row for row in matched if row["selected"] is not None)
            expected_selections.extend((target["articles"][0], target["qty"])
                                       for target in targets if target["kind"] == "exact")

    # Export actual exact selections; never promote cross/description candidates.
    assert len(selected_rows) == 6
    assert all(row["status"] == "exact" for row in selected_rows)
    assert Counter((row["selected"]["article"], row["qty"]) for row in selected_rows) == Counter(expected_selections)
    for number, row in enumerate(selected_rows, 1):
        row["line"] = number
    before = copy.deepcopy(selected_rows)
    xlsx = export_xlsx(selected_rows, customer="ДЕМО офлайн")
    docx = proposal_docx(selected_rows, customer="ДЕМО офлайн")
    assert selected_rows == before

    with io.BytesIO(xlsx) as stream:
        book = load_workbook(stream, data_only=False)
        try:
            values = list(book.active.values)
            start = next(i for i, row in enumerate(values) if row[0] == "№")
            exported = [dict(zip(values[start], row)) for row in values[start + 1:start + 1 + len(selected_rows)]]
            assert Counter((row["Артикул"], row["Количество"]) for row in exported) == Counter(expected_selections)
            assert all(row["Включено в предложение"] == "Да" for row in exported)
            assert all(row["Статус"] == "Точное совпадение" for row in exported)
        finally:
            book.close()
    document = Document(io.BytesIO(docx))
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.columns) == 9
    assert len(table.rows) == len(selected_rows) + 1
    labels = [cell.text for cell in table.rows[0].cells]
    assert labels == ["№", "Артикул / наименование", "Кол-во", "Цена за ед.\nс НДС",
                      "Сумма\nс НДС", "НДС\nставка / сумма", "Наличие", "Срок поставки", "Статус"]
    fields = [dict(zip(labels, [cell.text for cell in row.cells])) for row in table.rows[1:]]
    assert Counter((row["Артикул / наименование"].split(" — ", 1)[0], float(row["Кол-во"]))
                   for row in fields) == Counter(expected_selections)
    assert all(row["Статус"] == "Точное совпадение\nВключено" for row in fields)

    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    assert any("ЧЕРНОВИК" in text for text in paragraphs)
    appendix_start = paragraphs.index("Источники") + 1
    source_lines = [text for text in paragraphs[appendix_start:] if text.startswith("[")]
    sources = {}
    for text in source_lines:
        match = re.fullmatch(r"\[(\d+)\] (.+); строка источника: (.+)", text)
        assert match is not None, text
        reference, source, source_row = match.groups()
        assert reference not in sources
        sources[reference] = (source, source_row)
    expected_sources = {(row["selected"]["source"], str(row["selected"]["source_row"]))
                        for row in selected_rows}
    assert set(sources.values()) == expected_sources
    assert len(sources) == len(expected_sources)
    for exported_row, selected_row in zip(fields, selected_rows):
        selected = selected_row["selected"]
        description = exported_row["Артикул / наименование"]
        assert exported_row["№"] == str(selected_row["line"])
        assert description.startswith(f"{selected['article']} — {selected['name']}\n")
        assert f"\nЗапрос: {selected_row['query']}\n" in description
        reference = re.search(r"; \[(\d+)\]$", description)
        assert reference is not None, description
        assert sources[reference.group(1)] == (selected["source"], str(selected["source_row"]))
    for payload in (xlsx, docx):
        with ZipFile(io.BytesIO(payload)) as archive:
            assert archive.testzip() is None
    # Also catches attempts swallowed by a library instead of propagating.
    assert attempts == []

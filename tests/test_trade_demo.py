"""Run real catalog/request APIs, then perturb fixtures to prove honest scoring."""

import csv
import importlib.util
import io
import json
from pathlib import Path
import shutil
import socket

import pymupdf
import pytest

from kbagent import trade_demo
from kbagent.trade_catalog import normalize_article
from kbagent.trade_requests import read_file


@pytest.fixture
def demo_copy(tmp_path):
    return Path(shutil.copytree(trade_demo.DEMO_ROOT, tmp_path / "demo"))


def _edit_expected(root, case, change):
    path = root / case / "expected.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    change(expected)
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")


def test_real_offline_evaluation_is_isolated_and_repeatable(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Demo must not use network or OCR")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", forbidden)
    before = {p: p.read_bytes() for p in trade_demo.DEMO_ROOT.rglob("*") if p.is_file()}
    first = trade_demo.evaluate()
    assert first == trade_demo.evaluate()
    assert first["passed"] is True
    assert first["failed_checks"] == 0
    assert first["demo"] and first["offline"]
    expected_totals = {
        "exact_accuracy": 5, "cross_candidate_coverage": 3,
        "description_candidate_coverage": 5, "drawing_text_article_accuracy": 1,
        "no_data_accuracy": 3,
    }
    assert first["metrics"] == {key: {"correct": count, "total": count, "value": 1.0}
                                for key, count in expected_totals.items()}
    assert len(first["imports"]) == 3
    assert all(i["imported"] == 3 and i["rejected"] == 0 for i in first["imports"])
    assert before == {p: p.read_bytes() for p in trade_demo.DEMO_ROOT.rglob("*") if p.is_file()}


@pytest.mark.parametrize("case", trade_demo.CASES)
def test_fixtures_use_canonical_headers_and_normalized_articles(case):
    folder = trade_demo.DEMO_ROOT / case
    rows = list(csv.DictReader(io.StringIO((folder / "catalog.csv").read_text(encoding="utf-8"))))
    assert set(rows[0]) == set(trade_demo.CATALOG_MAPPING)
    assert all(row["article"].startswith("DEMO-") for row in rows)
    assert all("ДЕМО" in row["name"] and "ДЕМО" in row["applicability"] for row in rows)
    parsed = read_file("request.csv", (folder / "request.csv").read_bytes())
    query = parsed["items"][0]["query"]
    assert query != rows[0]["article"]
    assert any("А" <= c <= "я" for c in query)
    assert normalize_article(query) == normalize_article(rows[0]["article"])
    assert len(parsed["items"]) == 4


@pytest.mark.parametrize("kind,metric,correct,total", [
    ("exact", "exact_accuracy", 4, 5),
    ("cross", "cross_candidate_coverage", 2, 3),
    ("description", "description_candidate_coverage", 4, 5),
])
def test_wrong_expected_article_actually_reduces_metric(demo_copy, kind, metric, correct, total):
    def change(expected):
        target = next(p for p in expected["positions"] if p["kind"] == kind)
        target["articles"][0] = "UNLISTED-ARTICLE"

    _edit_expected(demo_copy, "agri", change)
    report = trade_demo.evaluate(demo_copy)
    assert not report["passed"]
    assert report["metrics"][metric] == {"correct": correct, "total": total, "value": correct / total}
    assert report["failed_checks"] == 1


def test_unknown_query_that_matches_is_failure(demo_copy):
    path = demo_copy / "agri" / "request.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("квантовый телепортатор", "DEMO-AG-001"), encoding="utf-8")
    report = trade_demo.evaluate(demo_copy)
    assert not report["passed"]
    assert report["metrics"]["no_data_accuracy"] == {"correct": 2, "total": 3, "value": 2 / 3}


def test_missing_row_is_scored_and_extra_row_is_not_silently_zipped(demo_copy):
    path = demo_copy / "agri" / "request.csv"
    original = path.read_text(encoding="utf-8")
    path.write_text("\n".join(original.splitlines()[:-1]) + "\n", encoding="utf-8")
    missing = trade_demo.evaluate(demo_copy)
    assert not missing["passed"]
    assert missing["metrics"]["no_data_accuracy"]["correct"] == 2
    path.write_text(original + "DEMO-AG-001,1\n", encoding="utf-8")
    extra = trade_demo.evaluate(demo_copy)
    assert not extra["passed"]
    assert any(c["kind"] == "unexpected" for c in extra["checks"])


def test_wrong_quantity_is_a_hard_failure(demo_copy):
    _edit_expected(demo_copy, "auto", lambda e: e["positions"][0].update(qty=100))
    report = trade_demo.evaluate(demo_copy)
    assert not report["passed"]
    assert any(c.get("quantity_ok") is False for c in report["checks"])


def test_invalid_import_is_not_reported_as_success(demo_copy):
    path = demo_copy / "agri" / "catalog.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",1200,", ",-1,"), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        trade_demo.evaluate(demo_copy)


def test_drawing_pdf_contains_explicit_fields_and_specification_only():
    folder = trade_demo.DEMO_ROOT / "drawings"
    data = (folder / "drawing.pdf").read_bytes()
    parsed = read_file("drawing.pdf", data)
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))["drawing"]
    assert parsed["fields"] == expected["fields"]
    assert [(p["query"], p["qty"]) for p in parsed["items"]] == [("DEMO-DR-002", 2), ("DEMO-DR-003", 4)]
    assert "ДЕМО" in parsed["text"]
    assert "Геометрия не сравнивается" in parsed["text"]
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        assert len(pdf) == 1
        assert not pdf[0].get_images()


def test_drawing_designation_is_scored_from_pdf_not_fixture_answer(demo_copy):
    path = demo_copy / "drawings" / "drawing.txt"
    path.write_text(path.read_text(encoding="utf-8").replace("DEMO-DR-001", "UNKNOWN-777"), encoding="utf-8")
    trade_demo.generate_drawing(path.parent)
    report = trade_demo.evaluate(demo_copy)
    assert not report["passed"]
    assert report["metrics"]["drawing_text_article_accuracy"] == {"correct": 0, "total": 1, "value": 0}
    assert any(c["kind"] == "drawing_fields" and not c["passed"] for c in report["checks"])


def test_no_samples_is_not_claimed_as_perfect_accuracy():
    assert trade_demo._metric(0, 0) == {"correct": 0, "total": 0, "value": None}


def test_cli_success_and_failure_codes_and_utf8_report(demo_copy, tmp_path, capsys):
    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_trade.py"
    spec = importlib.util.spec_from_file_location("eval_trade_demo_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "report.json"
    assert module.main(["--data", str(demo_copy), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] and "ДЕМО" in report["label"]
    _edit_expected(demo_copy, "auto", lambda e: e["positions"][0].update(articles=["WRONG"]))
    assert module.main(["--data", str(demo_copy)]) == 1
    assert module.main(["--data", str(tmp_path / "missing")]) == 1
    assert "evaluation failed" in capsys.readouterr().err

import json
import sqlite3
from contextlib import closing

import pytest

from kbagent import trade_sync
from scripts.sync_trade import main


def setup_source(tmp_path):
    source = tmp_path / "prices.csv"
    source.write_text("Article;Name;Price;Stock\nA1;Part;100;4\n", encoding="utf-8")
    config = tmp_path / "sync.json"
    entries = [{"path": str(source), "mapping": {"article": "Article", "name": "Name",
                "price": "Price", "stock": "Stock"}}]
    config.write_text(json.dumps(entries), encoding="utf-8")
    return source, config, tmp_path / "trade.sqlite", entries


def test_import_skip_and_missing_database(tmp_path):
    _, config, db, _ = setup_source(tmp_path)
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "skipped"
    other = tmp_path / "other.sqlite"
    assert trade_sync.sync(config, other)["sources"][0]["status"] == "imported"
    db.unlink()
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"


def test_mapping_change_and_missing_report_reimport(tmp_path):
    _, config, db, entries = setup_source(tmp_path)
    trade_sync.sync(config, db)
    entries[0]["mapping"].pop("stock")
    config.write_text(json.dumps(entries), encoding="utf-8")
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"
    config.with_suffix(".last.json").unlink()
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"
    config.with_suffix(".last.json").write_text("broken", encoding="utf-8")
    assert trade_sync.sync(config, db)["ok"]


def test_failure_preserves_catalog_and_does_not_cache_failed_hash(tmp_path):
    source, config, db, _ = setup_source(tmp_path)
    first = trade_sync.sync(config, db)["sources"][0]["last_success"]
    source.write_text("Wrong;Headers\n1;2\n", encoding="utf-8")
    failed = trade_sync.sync(config, db)
    assert not failed["ok"]
    assert failed["sources"][0]["last_success"] == first
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "error"
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT article,price,stock FROM items").fetchall() == [("A1", "100", "4")]


def test_modified_during_read_is_rejected(tmp_path, monkeypatch):
    _, config, db, _ = setup_source(tmp_path)
    trade_sync.sync(config, db)
    actual = trade_sync.os.fstat
    calls = []

    def changed(fd):
        stat = actual(fd)
        calls.append(fd)
        if len(calls) == 2:
            class Changed:
                st_dev, st_ino, st_size = stat.st_dev, stat.st_ino, stat.st_size
                st_mtime_ns, st_ctime_ns = stat.st_mtime_ns + 1, stat.st_ctime_ns
            return Changed()
        return stat

    monkeypatch.setattr(trade_sync.os, "fstat", changed)
    result = trade_sync.sync(config, db)
    assert not result["ok"]
    assert "changed during read" in result["sources"][0]["error"]


def test_missing_sources_invalid_config_and_cli_report(tmp_path):
    source, config, db, entries = setup_source(tmp_path)
    source.unlink()
    report = tmp_path / "report.json"
    assert main(["--config", str(config), "--db", str(db), "--report", str(report)]) == 1
    assert not json.loads(report.read_text(encoding="utf-8"))["ok"]
    entries[0]["path"] = "relative.csv"
    config.write_text(json.dumps(entries), encoding="utf-8")
    assert "absolute" in trade_sync.sync(config, db)["sources"][0]["error"]
    config.unlink()
    assert not trade_sync.sync(config, db)["ok"]


def test_xlsx_sheet_and_updated_content(tmp_path):
    from openpyxl import Workbook
    _, config, db, entries = setup_source(tmp_path)
    source = tmp_path / "export.xlsx"
    book = Workbook()
    book.active.title = "Prices"
    book.active.append(["Article", "Name", "Price", "Stock"])
    book.active.append(["B2", "Part", 200, 8])
    book.save(source)
    entries[0].update(path=str(source), sheet="Prices", catalog="Supplier")
    config.write_text(json.dumps(entries), encoding="utf-8")
    assert trade_sync.sync(config, db)["ok"]
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "skipped"
    book.active.cell(2, 3, 250)
    book.save(source)
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT article,price,stock FROM items").fetchall() == [("B2", "250", "8")]


def test_invalid_row_rolls_back_entire_catalog(tmp_path):
    source, config, db, _ = setup_source(tmp_path)
    trade_sync.sync(config, db)
    source.write_text("Article;Name;Price;Stock\nA1;Part;999;1\nB2;Bad;invalid;2\n", encoding="utf-8")
    result = trade_sync.sync(config, db)
    assert not result["ok"] and result["errors"]
    assert result["sources"][0]["result"]["imported"] == 0
    assert result["sources"][0]["result"]["errors"][0]["source_row"] == 3
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT article,price,stock FROM items").fetchall() == [("A1", "100", "4")]


def test_missing_source_keeps_success_and_empty_database_is_not_skipped(tmp_path):
    source, config, db, _ = setup_source(tmp_path)
    original = source.read_bytes()
    trade_sync.sync(config, db)
    source.unlink()
    assert trade_sync.sync(config, db)["sources"][0]["last_success"]
    source.write_bytes(original)
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "skipped"
    with closing(sqlite3.connect(db)) as connection:
        with connection:
            connection.execute("DELETE FROM items")
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "imported"


def test_report_publication_failure_keeps_previous_json(tmp_path, monkeypatch):
    report = tmp_path / "last.json"
    trade_sync.write_report(report, {"ok": True})

    def fail(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(trade_sync.os, "replace", fail)
    with pytest.raises(OSError, match="publication failed"):
        trade_sync.write_report(report, {"ok": False})
    assert json.loads(report.read_text(encoding="utf-8")) == {"ok": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_original_archived_after_success_only(tmp_path, monkeypatch):
    from kbagent.trade_sources import read

    source, config, db, _ = setup_source(tmp_path)
    original = source.read_bytes()
    calls = []
    archive = trade_sync.archive

    def archive_after_commit(root, data):
        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        calls.append((root, data))
        return archive(root, data)

    monkeypatch.setattr(trade_sync, "archive", archive_after_commit)
    report = trade_sync.sync(config, db)
    digest = report["sources"][0]["source_sha256"]
    assert digest == report["sources"][0]["sha256"]
    assert read(db.parent, digest) == original
    assert calls == [(db.parent, original)]
    assert trade_sync.sync(config, db)["sources"][0]["status"] == "skipped"
    source.write_text("Article;Name;Price;Stock\nA1;Part;invalid;4\n", encoding="utf-8")
    assert not trade_sync.sync(config, db)["ok"]
    assert calls == [(db.parent, original)]

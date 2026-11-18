"""One scheduled pass over explicitly configured local CSV/XLSX exports.

Configuration is a JSON list of {path, mapping, catalog?, sheet?}. Paths must
be absolute files; no URLs, folder discovery or wildcard expansion is used.
The last report is stored as <config stem>.last.json beside the configuration.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .trade_catalog import TradeCatalog


def write_report(path: Path, report: dict) -> None:
    """Publish a complete JSON report with an atomic same-directory replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _stamp(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_stable(path: Path) -> bytes:
    before = path.stat()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    if not (_stamp(before) == _stamp(opened) == _stamp(after) == _stamp(path.stat())):
        raise ValueError("source changed during read; retry after export completes")
    return data


def _present(db_path: Path, success: dict, digest: str, catalog: str) -> bool:
    if not db_path.is_file() or not success.get("import_id"):
        return False
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT 1 FROM imports i WHERE i.id=? AND i.sha256=? AND i.catalog=? "
                "AND EXISTS (SELECT 1 FROM items WHERE import_id=i.id)",
                (success["import_id"], digest, catalog),
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def sync(config_path: Path, db_path: Path) -> dict:
    """Import changed exports; report per-source failures without losing successes.

    Imports use TradeCatalog's transactional catalog replacement. Successful
    fingerprints include mapping, sheet, catalog and target DB, not only bytes.
    """
    config_path, db_path = Path(config_path).resolve(), Path(db_path).resolve()
    last_path = config_path.with_suffix(".last.json")
    if last_path == config_path or db_path in (config_path, last_path):
        raise ValueError("configuration, database and report paths must be distinct")
    report = {"time": datetime.now(timezone.utc).isoformat(), "ok": True,
              "sources": [], "errors": [], "imported": 0, "skipped": 0}
    try:
        previous = json.loads(last_path.read_text(encoding="utf-8"))
        old = {r["key"]: r for r in previous.get("sources", []) if isinstance(r, dict) and "key" in r}
    except (OSError, ValueError, TypeError, AttributeError):
        old = {}
    try:
        sources = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(sources, list) or not sources:
            raise ValueError("configuration must be a nonempty JSON list")
    except (OSError, ValueError) as exc:
        report.update(ok=False, errors=[str(exc)], sources=list(old.values()))
        write_report(last_path, report)
        return report
    for number, source in enumerate(sources):
        row = {"key": str(number), "status": "error"}
        try:
            if not isinstance(source, dict):
                raise ValueError("source must be an object")
            raw_path = source.get("path")
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or any(c in raw_path for c in "*?\x00"):
                raise ValueError("path must be an explicit absolute local file path")
            path = Path(raw_path).resolve()
            mapping = source.get("mapping")
            catalog, sheet = source.get("catalog", "Основной"), source.get("sheet")
            if not isinstance(mapping, dict) or not mapping or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
                raise ValueError("mapping must be a nonempty string-to-string object")
            if not isinstance(catalog, str) or not catalog.strip() or (sheet is not None and not isinstance(sheet, str)):
                raise ValueError("catalog must be nonempty text; sheet must be text or null")
            identity = {"path": str(path), "mapping": mapping, "catalog": catalog,
                        "sheet": sheet, "db": str(db_path)}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            row.update(key=key, path=str(path), catalog=catalog)
            success = old.get(key, {}).get("last_success", {})
            if not isinstance(success, dict):
                success = {}
            if success:
                row["last_success"] = success
            if path.suffix.lower() not in (".csv", ".xlsx") or not path.is_file():
                raise ValueError("source must be an existing CSV/XLSX file")
            data = _read_stable(path)
            digest = hashlib.sha256(data).hexdigest()
            row["sha256"] = digest
            if success.get("sha256") == digest and _present(db_path, success, digest, catalog):
                row["status"] = "skipped"
            else:
                result = TradeCatalog(db_path).import_table(path.name, data, mapping, catalog=catalog, sheet=sheet)
                row["result"] = result
                if result.get("errors") or not result.get("imported"):
                    raise ValueError("import rejected: invalid rows or no valid rows")
                row.update(status="imported", last_success={"sha256": digest,
                           "import_id": result["import_id"], "time": report["time"]})
        except Exception as exc:  # isolate one export failure from the remaining sources
            row["error"] = str(exc)
            if isinstance(getattr(exc, "report", None), dict):
                row["result"] = exc.report
            report["ok"] = False
            report["errors"].append({"source": number, "error": str(exc)})
        report["sources"].append(row)
    report.update(imported=sum(r["status"] == "imported" for r in report["sources"]),
                  skipped=sum(r["status"] == "skipped" for r in report["sources"]))
    write_report(last_path, report)
    return report

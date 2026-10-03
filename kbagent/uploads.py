"""Документы, загруженные пользователем через чат: сохранение, индексация, удаление."""
import re
from pathlib import Path

import yaml

from .config import settings
from .ingest import load_catalog
from .store import Index

UPLOAD_DIR = settings.raw_dir / "uploaded"
CATALOG = UPLOAD_DIR / "catalog.yaml"


def _read_catalog() -> list[dict]:
    if CATALOG.exists():
        return yaml.safe_load(CATALOG.read_text(encoding="utf-8")).get("documents", [])
    return []


def _write_catalog(docs: list[dict]) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(yaml.safe_dump({"documents": docs}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def doc_id_for(filename: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", Path(filename).stem).strip("_")[:60]
    return f"upl_{stem or 'document'}"


def add_upload(index: Index, filename: str, data: bytes) -> tuple[str, str, int]:
    """Сохраняет файл, добавляет его в каталог и индекс. Возвращает id, название и число фрагментов."""
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise ValueError("поддерживаются PDF и DOCX")
    doc_id = doc_id_for(filename)
    title = Path(filename).stem.replace("_", " ")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for old in UPLOAD_DIR.glob(f"{doc_id}.*"):
        if old.suffix.lower() in (".pdf", ".docx"):
            old.unlink()
    path = UPLOAD_DIR / f"{doc_id}{ext}"
    path.write_bytes(data)

    docs = [d for d in _read_catalog() if d["id"] != doc_id]
    docs.append({"id": doc_id, "title": title, "kind": "Загруженный документ", "access": "all"})
    _write_catalog(docs)

    chunks = index.add_document(path, load_catalog(settings.raw_dir), settings.index_dir)
    return doc_id, title, len(chunks)


def list_uploads() -> list[dict]:
    return _read_catalog()


def remove_upload(index: Index, doc_id: str) -> None:
    index.remove_document(doc_id, settings.index_dir)
    for p in UPLOAD_DIR.glob(f"{doc_id}.*"):
        p.unlink()
    _write_catalog([d for d in _read_catalog() if d["id"] != doc_id])

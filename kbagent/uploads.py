"""Документы, загруженные через чат: сохранение, индексация, утверждение, замена редакции, удаление.

Загружать и удалять документы может только администратор (проверяется в интерфейсе).
Загруженный документ сразу участвует в поиске, но до утверждения помечается
«загружен пользователем, не утверждён» — и в ссылке на источник, и предупреждением под ответом.
Если документ загружен как новая редакция другого («заменяет»), старая редакция
отменяется и перестаёт участвовать в поиске; при удалении новой — возвращается.
"""
import re
import time
from pathlib import Path

import yaml

from .config import settings
from .ingest import access_of, load_catalog
from .store import Index

UPLOAD_DIR = settings.raw_dir / "uploaded"
CATALOG = UPLOAD_DIR / "catalog.yaml"

KIND_PENDING = "Загруженный документ"
KIND_APPROVED = "Загруженный документ (утверждён)"


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


def _sync_access(index: Index, doc_id: str) -> None:
    """Приводит доступ документа в индексе к каталогу (действует / отменён)."""
    info = load_catalog(settings.raw_dir).get(doc_id)
    if info is not None:
        index.update_meta(doc_id, settings.index_dir, access=access_of(info))


def add_upload(index: Index, filename: str, data: bytes, access: str = "all", user: str | None = None,
               replaces: str | None = None, progress=None) -> dict:
    """Сохраняет файл, добавляет его в каталог и индекс.
    Возвращает id, title, chunks, replaced_same (перезаписан одноимённый), ocr_pages, scanned_pages."""
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise ValueError("поддерживаются PDF и DOCX")
    doc_id = doc_id_for(filename)
    title = Path(filename).stem.replace("_", " ")
    docs = _read_catalog()
    replaced_same = any(d["id"] == doc_id for d in docs)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for old in UPLOAD_DIR.glob(f"{doc_id}.*"):
        if old.suffix.lower() in (".pdf", ".docx"):
            old.unlink()
    path = UPLOAD_DIR / f"{doc_id}{ext}"
    path.write_bytes(data)

    entry = {"id": doc_id, "title": title, "kind": KIND_PENDING, "access": access,
             "uploaded_by": user, "uploaded_at": time.strftime("%Y-%m-%d %H:%M"), "approved": False}
    if replaces and replaces != doc_id:
        entry["replaces"] = replaces
    _write_catalog([d for d in docs if d["id"] != doc_id] + [entry])

    stats: dict = {}
    chunks = index.add_document(path, load_catalog(settings.raw_dir), settings.index_dir,
                                progress=progress, stats=stats)
    if replaces:
        _sync_access(index, replaces)
    return {"id": doc_id, "title": title, "chunks": len(chunks), "replaced_same": replaced_same, **stats}


def approve_upload(index: Index, doc_id: str, user: str | None = None) -> None:
    docs = _read_catalog()
    for d in docs:
        if d["id"] == doc_id:
            d.update(approved=True, kind=KIND_APPROVED, approved_by=user,
                     approved_at=time.strftime("%Y-%m-%d %H:%M"))
    _write_catalog(docs)
    index.update_meta(doc_id, settings.index_dir, doc_kind=KIND_APPROVED)


def list_uploads() -> list[dict]:
    return _read_catalog()


def remove_upload(index: Index, doc_id: str) -> None:
    docs = _read_catalog()
    replaced = next((d.get("replaces") for d in docs if d["id"] == doc_id), None)
    _write_catalog([d for d in docs if d["id"] != doc_id])
    index.remove_document(doc_id, settings.index_dir)
    for p in UPLOAD_DIR.glob(f"{doc_id}.*"):
        if p.suffix.lower() in (".pdf", ".docx"):
            p.unlink()
    if replaced:
        _sync_access(index, replaced)   # старая редакция снова действует


def documents(index: Index) -> list[dict]:
    """Все документы базы знаний со статусом — для списка у администратора."""
    catalog = load_catalog(settings.raw_dir)
    present = index.doc_ids()
    out = []
    for doc_id, info in catalog.items():
        if doc_id in present:
            out.append({"id": doc_id, "title": info.get("title", doc_id), "kind": info.get("kind", ""),
                        "access": info.get("access", "all"),
                        "status": "отменён" if access_of(info) == "archive" else "действует",
                        "replaced_by": info.get("replaced_by")})
    return out

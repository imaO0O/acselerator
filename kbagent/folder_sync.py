"""Папка документов отдела → база знаний: новые и изменённые файлы подключаются сами, удалённые — убираются.

Источник — сетевая или локальная папка (KB_DOCS_FOLDER), где отдел и так хранит документы. Файлы копируются
в data/raw/folder/: ссылка в ответе открывает страницу с выделенным фрагментом и тогда, когда сетевая папка
недоступна, а пересборка индекса (scripts/build_index.py) видит те же документы. Excel и изображения
преобразуются так же, как при загрузке через администрирование.

Что подключено и в каком виде — data/raw/folder/sync_state.json (размер, время изменения и хэш каждого файла):
файл с тем же содержимым повторно не разбирается. Название, версия, срок действия, владелец и доступ — из реестра:
catalog.yaml в самой папке или реестр, загруженный scripts/import_partner.py (по имени файла); если записи нет —
название из самого документа, доступ — KB_DOCS_FOLDER_ACCESS.

Защита от сбоя сети: если папка недоступна или из неё пропала большая часть файлов, документы не удаляются —
в отчёте сказано почему. Каждое изменение — в журнале действий.

    python scripts/sync_folder.py                       — один проход
    python scripts/sync_folder.py --watch 600           — проверять каждые 10 минут
В чате — «Администрирование» → «Документы» → «Папка документов».
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .actions import log_action
from .config import settings
from .ingest import load_catalog

MIRROR = settings.raw_dir / "folder"
STATE = "sync_state.json"
CATALOG = "catalog.yaml"
SUPPORTED = (".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
KIND = "Документ из папки отдела"


@dataclass
class Report:
    source: str = ""
    started: str = ""
    seconds: float = 0.0
    added: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    unchanged: int = 0
    errors: list = field(default_factory=list)       # [файл, причина]
    warning: str = ""                                # почему удаление не выполнено

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def _scan(source: Path) -> dict[str, tuple[int, int]]:
    """Файлы папки: относительный путь → (размер, время изменения). Временные файлы Office (~$…) — мимо."""
    out = {}
    for p in sorted(source.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(("~$", ".")):
            st = p.stat()
            out[p.relative_to(source).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _doc_id(rel: str, taken: set[str]) -> str:
    """Имя файла без расширения — как в реестре партнёра; совпало с другим документом — с хэшем пути."""
    stem = re.sub(r"[^\w\-]+", "_", Path(rel).stem).strip("_") or "document"
    if stem in taken:
        stem = f"{stem[:52]}_{hashlib.sha1(rel.encode('utf-8')).hexdigest()[:7]}"
    return stem


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("documents", []) or []


def _registry(source: Path) -> dict[str, dict]:
    """Записи реестра по имени файла без расширения: реестр партнёра (scripts/import_partner.py), затем
    catalog.yaml в самой папке — он главнее. Каталоги демо-документов не смотрим: совпадение имени файла
    не должно подтянуть чужое название."""
    entries: dict[str, dict] = {}
    for path in (settings.raw_dir / "partner" / CATALOG, source / CATALOG):
        entries.update({str(d["id"]): d for d in _read(path)})
    return entries


def _load_state(mirror: Path) -> dict:
    try:
        return json.loads((mirror / STATE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"files": {}}


def last_report(mirror: Path | None = None) -> dict | None:
    return _load_state(mirror or MIRROR).get("last")


def sync(index, source: Path, index_dir: Path | None = None, mirror: Path | None = None, user: str = "папка",
         access: str | None = None, max_removed_share: float = 0.5) -> Report:
    """Один проход: подключить новые и изменённые файлы, убрать удалённые. index — store.Index чата или скрипта."""
    from .uploads import convert_upload, guess_title
    index_dir = index_dir or settings.index_dir
    mirror = mirror or MIRROR
    access = access or settings.docs_folder_access
    t0 = time.time()
    rep = Report(source=str(source), started=time.strftime("%Y-%m-%d %H:%M:%S"))
    state = _load_state(mirror)
    files_state: dict = state["files"]
    if not source.is_dir():
        rep.warning = f"папка «{source}» недоступна — изменения не применялись"
        return _finish(rep, state, mirror, t0)
    mirror.mkdir(parents=True, exist_ok=True)
    catalog_path = mirror / CATALOG
    catalog = {d["id"]: d for d in _read(catalog_path)}
    registry = _registry(source)
    found = _scan(source)
    taken = {c.doc_id for c in index.chunks} - {v["doc_id"] for v in files_state.values()}

    for rel, (size, mtime) in found.items():
        known = files_state.get(rel)
        if known and (known["size"], known["mtime"]) == (size, mtime):
            rep.unchanged += 1
            continue
        try:
            data = (source / rel).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if known and known["sha256"] == digest:          # файл «тронули», содержимое то же
                known.update(size=size, mtime=mtime)
                rep.unchanged += 1
                continue
            ext, data, _ = convert_upload(Path(rel).name, data)
            doc_id = known["doc_id"] if known else _doc_id(rel, taken | {v["doc_id"] for v in files_state.values()})
            for old in mirror.glob(f"{doc_id}.*"):
                old.unlink()
            path = mirror / f"{doc_id}{ext}"
            path.write_bytes(data)
            entry = dict(registry.get(Path(rel).stem) or registry.get(doc_id) or catalog.get(doc_id) or {})
            entry.update(id=doc_id, source_file=rel)
            if "title" not in entry:
                entry.update(title=guess_title(path) or Path(rel).stem.replace("_", " "), kind=KIND, access=access)
            catalog[doc_id] = entry
            _write_catalog(catalog_path, catalog)
            chunks = index.add_document(path, {**load_catalog(settings.raw_dir), **catalog}, index_dir)
            files_state[rel] = {"doc_id": doc_id, "size": size, "mtime": mtime, "sha256": digest,
                                "chunks": len(chunks)}
            (rep.updated if known else rep.added).append(rel)
            log_action("folder_sync", user, rel, f"{'обновлён' if known else 'добавлен'}; фрагментов: {len(chunks)}")
        except Exception as e:  # noqa: BLE001 — один повреждённый файл не останавливает остальные
            rep.errors.append([rel, f"{type(e).__name__}: {str(e)[:200]}"])

    gone = [rel for rel in files_state if rel not in found]
    if gone and len(gone) > 3 and len(gone) > max_removed_share * len(files_state):
        rep.warning = (f"из папки пропали {len(gone)} из {len(files_state)} файлов — похоже, она доступна не "
                       "полностью; документы не удалены. Если файлы удалены намеренно, удалите их из базы "
                       "на этой же вкладке по одному или запустите scripts/sync_folder.py --allow-mass-removal")
    else:
        for rel in gone:
            doc_id = files_state.pop(rel)["doc_id"]
            index.remove_document(doc_id, index_dir)
            for old in mirror.glob(f"{doc_id}.*"):
                old.unlink()
            catalog.pop(doc_id, None)
            rep.removed.append(rel)
            log_action("folder_sync", user, rel, "удалён из папки — убран из базы знаний")
        if gone:
            _write_catalog(catalog_path, catalog)
    return _finish(rep, state, mirror, t0)


def _write_catalog(path: Path, catalog: dict) -> None:
    path.write_text("# Документы из папки отдела (kbagent/folder_sync.py). Не править вручную — реестр задаётся "
                    "catalog.yaml в самой папке или scripts/import_partner.py.\n"
                    + yaml.safe_dump({"documents": list(catalog.values())}, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def _finish(rep: Report, state: dict, mirror: Path, t0: float) -> Report:
    rep.seconds = round(time.time() - t0, 1)
    state["last"] = {"started": rep.started, "source": rep.source, "seconds": rep.seconds, "added": len(rep.added),
                     "updated": len(rep.updated), "removed": len(rep.removed), "unchanged": rep.unchanged,
                     "errors": rep.errors[:20], "warning": rep.warning}
    if mirror.exists():
        (mirror / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep

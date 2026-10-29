"""Резервная копия данных заказчика и перенос в новую версию без потерь.

Код, модели и демо-документы приходят с поставкой; копируется только то, что накопилось у заказчика:
учётные записи и ключи, журналы, диалоги, оценки, решения эксперта, «Эффект», загруженные документы и папка
отдела, документы партнёра, справочник параметров, прайс, словарь и правила расчётов, индекс.

Обновление версии: в старой установке «create», новая поставка ставится рядом (install.bat), в новой —
«restore». Без этого новая версия начала бы с индекса, справочника и прайса из поставки (демо), а документы
заказчика пропали бы из поиска. Перед восстановлением текущее состояние сохраняется страховочной копией.

Архив — zip с manifest.json: версия кода, время, SHA-256 каждого файла; при восстановлении контрольные суммы
проверяются. В архиве хэши паролей и ключей и журналы вопросов — храните его по правилам службы безопасности.
"""
import hashlib
import json
import shutil
import socket
import subprocess
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path

from .config import ROOT

# что принадлежит заказчику; пути — от корня установки
STATE = ["logs", "data/settings.env", "data/users.yaml", "data/api_keys.yaml", "data/model_api_key.txt", "data/index",
         "data/raw/uploaded", "data/raw/folder", "data/raw/partner", "data/spravochnik/pribory.csv",
         "data/spravochnik/prajs.csv", "data/spravochnik/prajs_1c.json", "data/glossary.yaml", "data/pravila_raschetov.yaml", "data/tkp", "data/mail"]
SKIP = (".lock", ".tmp")                         # блокировка и недописанные файлы индекса
PORTS = {8501: "чат", 8000: "REST API"}


def _version(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %cd", "--date=format:%d.%m.%Y"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "неизвестна (не git)"


def _files(root: Path) -> list[Path]:
    out = []
    for rel in STATE:
        p = root / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in sorted(p.rglob("*")) if f.is_file() and not f.name.endswith(SKIP)
                    and ".tmp." not in f.name]
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create(root: Path = ROOT, out_dir: Path | None = None, keep: int = 10) -> Path:
    """Архив состояния. Индекс копируется под его блокировкой — без половины чужой записи."""
    from .store import _lock
    out_dir = out_dir or root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"agent_backup_{time.strftime('%Y-%m-%d_%H%M%S')}.zip"
    manifest = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "version": _version(root), "paths": [], "files": {}}
    index_dir = root / "data" / "index"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        with _lock(index_dir) if index_dir.exists() else nullcontext():
            for f in _files(root):
                rel = f.relative_to(root).as_posix()
                data = f.read_bytes()
                z.writestr(rel, data)
                manifest["files"][rel] = _sha(data)
        manifest["paths"] = [rel for rel in STATE if (root / rel).exists()]
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    old = sorted(out_dir.glob("agent_backup_*.zip"))[:-keep] if keep else []
    for p in old:
        p.unlink()
    return path


def verify(path: Path) -> dict:
    """manifest.json архива; ValueError — если файл повреждён или изменён."""
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        for rel, digest in manifest["files"].items():
            if _sha(z.read(rel)) != digest:
                raise ValueError(f"контрольная сумма не совпала: {rel}")
    return manifest


def running(ports: dict = PORTS) -> list[str]:
    """Что из агента сейчас запущено на этом компьютере — восстанавливать поверх работающего нельзя."""
    busy = []
    for port, name in ports.items():
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(f"{name} (порт {port})")
    return busy


def restore(path: Path, root: Path = ROOT, safety_dir: Path | None = None) -> tuple[dict, Path]:
    """Восстановить состояние из архива: сначала проверка и страховочная копия текущего, затем замена каждой
    части из архива целиком (папка индекса из архива — вместо текущей, без смешивания). Части, которых в архиве
    нет, не трогаются. Возвращает manifest и путь страховочной копии."""
    manifest = verify(path)
    safety = create(root, safety_dir or root / "backups" / "before_restore", keep=5)
    with zipfile.ZipFile(path) as z:
        for rel in manifest["paths"]:
            target = root / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for rel in manifest["files"]:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(rel))
    return manifest, safety


FOLDERS = {"uploaded": "загружено через чат", "folder": "из папки отдела", "partner": "документы партнёра"}


def summary(manifest: dict) -> str:
    """«версия …, создана …: файлов — 57; документов: загружено через чат — 3, из папки отдела — 12»."""
    counts: dict[str, int] = {}
    for rel in manifest["files"]:
        parts = rel.split("/")
        if len(parts) == 4 and parts[:2] == ["data", "raw"] and parts[3].lower().endswith((".pdf", ".docx")):
            counts[parts[2]] = counts.get(parts[2], 0) + 1
    docs = ", ".join(f"{FOLDERS.get(k, k)} — {n}" for k, n in sorted(counts.items()))
    return (f"версия {manifest['version']}, создана {manifest['created']}: файлов — {len(manifest['files'])}"
            + (f"; документов: {docs}" if docs else ""))

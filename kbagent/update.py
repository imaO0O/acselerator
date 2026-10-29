"""Обновление одной командой: данные заказчика из старой установки — в новую, с возвратом, если что-то не так.

Новая поставка ставится в другую папку (install.bat), затем в ней: python scripts\\update.py --from D:\\agent_old.
Шаги: задачи по расписанию снимаются (иначе сторож тут же поднял бы старый чат), чат, API и модели старой установки
останавливаются; из неё делается резервная копия — учётные записи, журналы, диалоги, документы, справочник, прайс,
индекс, настройки и сертификат; копия проверяется по контрольным суммам и восстанавливается в новой (текущее
состояние новой — в страховочную копию); задачи регистрируются уже на новую папку, сторож запускает модели и чат.
Старая установка только читается: если шаг не удался, задачи возвращаются на неё и она запускается, как была.
Вручную было пять шагов в двух папках — и ошибка любого (restore поверх работающего чата, забытая регистрация
задач) молча ломала систему у заказчика.
"""
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import backup
from .actions import log_action
from .config import ROOT

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PORTS = (8501, 8000)                    # чат и REST API


class UpdateError(Exception):
    pass


def _powershell(script: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess | None:
    if sys.platform != "win32" or not script.exists():
        return None
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                          capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def stop_services(root: Path) -> None:
    """Чат и API — по портам, модели — stop_servers этой установки."""
    if sys.platform == "win32":
        ports = ",".join(str(p) for p in PORTS)
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-NetTCPConnection -State Listen -LocalPort {ports} -ErrorAction SilentlyContinue | "
                        "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"],
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    _powershell(root / "scripts" / "stop_servers.ps1", timeout=60)


def set_tasks(root: Path, on: bool) -> None:
    """Задачи по расписанию — на эту установку (имена KBAgent-… общие: регистрация заменяет прежние) или снять."""
    _powershell(root / "scripts" / "install_tasks.ps1", *([] if on else ["-Remove"]))


def start_services(root: Path) -> None:
    """Один проход сторожа этой установки: модели, чат, API (если включён)."""
    python = root / ".venv" / "Scripts" / "python.exe"
    script = root / "scripts" / "watchdog.py"
    if script.exists():
        subprocess.run([str(python if python.exists() else sys.executable), str(script)], cwd=root,
                       capture_output=True, timeout=600, creationflags=NO_WINDOW)


@dataclass
class Ops:
    stop: Callable[[Path], None] = stop_services
    tasks: Callable[[Path, bool], None] = set_tasks
    start: Callable[[Path], None] = start_services


def looks_installed(root: Path) -> bool:
    return (root / "kbagent").is_dir() and (root / "data").is_dir()


def migrate(old: Path, new: Path = ROOT, ops: Ops | None = None, log: Callable[[str], None] = print,
            user: str | None = None) -> dict:
    ops = ops or Ops()
    old, new = old.resolve(), new.resolve()
    if old == new:
        raise UpdateError("укажите папку прежней установки, а не эту")
    if not looks_installed(old):
        raise UpdateError(f"в «{old}» нет установки агента (нужны папки kbagent и data)")
    log("1. Снимаю задачи по расписанию, чтобы сторож не поднял старый чат…")
    ops.tasks(old, False)
    log("2. Останавливаю чат, REST API и модели прежней установки…")
    ops.stop(old)
    if busy := backup.running():
        ops.tasks(old, True)
        raise UpdateError("не остановились: " + ", ".join(busy) + " — закройте их и повторите; прежняя установка "
                          "оставлена как была")
    try:
        log("3. Резервная копия прежней установки…")
        archive = backup.create(root=old)
        manifest = backup.verify(archive)
        log(f"   {archive} — {backup.summary(manifest)}")
        log("4. Переношу данные в новую установку…")
        manifest, safety = backup.restore(archive, root=new)
        log("5. Задачи по расписанию — на новую папку…")
        ops.tasks(new, True)
        log("6. Запускаю модели и чат…")
        ops.start(new)
    except Exception as e:  # noqa: BLE001 — любой сбой: прежняя установка снова работает
        log(f"Не получилось: {e}. Возвращаю прежнюю установку…")
        ops.tasks(old, True)
        ops.start(old)
        raise UpdateError(f"обновление не выполнено ({e}); прежняя установка запущена, как была") from e
    log_action("update", user, str(old), f"данные перенесены в {new}; {backup.summary(manifest)}")
    return {"archive": archive, "safety": safety, "summary": backup.summary(manifest)}

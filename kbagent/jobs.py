"""Фоновые задачи по расписанию: резервная копия, почта отдела, папка документов, цены из 1С.

Без них почту, папку и 1С кто-то должен проверять вручную или держать открытыми окна с --watch, а резервная
копия делается, только когда о ней вспомнят. scripts/install_tasks.bat регистрирует задачи в Планировщике
заданий Windows; каждая запускает scripts/run_job.py <задача>, а он — скрипт задачи и пишет итог в
logs/jobs/<задача>.json: когда, сколько длилось, код, последние строки вывода. Вкладка «Система» показывает по
каждой задаче, когда она последний раз отработала, не отстала ли и есть ли она в Планировщике.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, settings

SCRIPTS = ROOT / "scripts"
TASK_PREFIX = "KBAgent-"                # имена задач в Планировщике: KBAgent-backup, KBAgent-mail…
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # без мелькающего окна консоли каждые 5 минут


@dataclass(frozen=True)
class Job:
    key: str
    title: str
    script: str
    args: tuple[str, ...]
    every: int                          # минут между запусками
    late: int                           # минут без успешного запуска — уже предупреждение
    timeout: int = 30 * 60              # секунд; дольше — запуск прерывается
    logon: bool = False                 # ещё и при входе в Windows (автозапуск)


JOBS = {j.key: j for j in [
    Job("backup", "Резервная копия", "backup.py", ("create",), 24 * 60, 2 * 24 * 60),
    Job("mail", "Почта отдела", "check_mail.py", (), 5, 30, 10 * 60),
    Job("folder", "Папка документов отдела", "sync_folder.py", (), 10, 60),
    Job("1c", "Цены из 1С", "sync_1c.py", (), 60, 6 * 60, 10 * 60),
    # автозапуск и перезапуск упавшего: модели, чат, API (kbagent/watchdog.py)
    Job("watchdog", "Сторож: автозапуск и перезапуск", "watchdog.py", (), 2, 10, 6 * 60, logon=True),
]}


def configured(key: str) -> bool:
    if key == "mail":
        from . import mailbox
        return mailbox.configured()
    if key == "folder":
        return bool(settings.docs_folder)
    if key == "1c":
        from . import onec
        return onec.configured()
    return True


def _path(key: str) -> Path:
    return settings.log_dir / "jobs" / f"{key}.json"


def last(key: str) -> dict | None:
    try:
        return json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record(key: str, data: dict) -> None:
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _python() -> str:
    # Планировщик запускает pythonw (без окна); сам скрипт задачи — обычным python, чтобы был вывод
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)


def run(key: str) -> dict:
    """Один запуск задачи; итог — в logs/jobs/<задача>.json (и тот же словарь — в ответ)."""
    job = JOBS[key]
    prev = last(key) or {}
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    _record(key, {**prev, "running": True, "started": started})
    t0 = time.time()
    try:
        proc = subprocess.run([_python(), str(SCRIPTS / job.script), *job.args], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=job.timeout,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=NO_WINDOW)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, output = -1, f"прервано: дольше {job.timeout // 60} мин"
    except OSError as e:
        code, output = -1, f"не удалось запустить: {e}"
    lines = [x for x in output.splitlines() if x.strip()]
    rec = {"running": False, "started": started, "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
           "seconds": round(time.time() - t0, 1), "code": code, "ok": code == 0,
           "message": lines[-1][:300] if lines else "", "output": "\n".join(lines[-15:])[-3000:],
           "last_ok": started if code == 0 else prev.get("last_ok")}
    _record(key, rec)
    return rec


def _age_minutes(stamp: str | None, now: float) -> float | None:
    if not stamp:
        return None
    try:
        return (now - time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))) / 60
    except ValueError:
        return None


def newest_backup() -> Path | None:
    archives = sorted((ROOT / "backups").glob("agent_backup_*.zip"), key=lambda p: p.stat().st_mtime)
    return archives[-1] if archives else None


def status(now: float | None = None, registered: dict[str, bool] | None = None) -> list[dict]:
    """По каждой задаче: настроена ли, когда последний успешный запуск, итог последнего, состояние.

    Состояние: ok, late (давно не было успешного запуска), error (последний запуск с ошибкой), never (ещё не
    запускалась), off (не настроена — почта, папка или 1С не заданы), running.
    """
    now = now or time.time()
    out = []
    for job in JOBS.values():
        rec = last(job.key) or {}
        last_ok = rec.get("last_ok")
        if job.key == "backup" and (archive := newest_backup()):
            # копия, сделанная вручную на вкладке «Система» или скриптом, тоже считается
            made = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(archive.stat().st_mtime))
            last_ok = max(filter(None, [last_ok, made]))
        age = _age_minutes(last_ok, now)
        if not configured(job.key):
            state = "off"
        elif rec.get("running") and (_age_minutes(rec.get("started"), now) or 0) < job.timeout / 60 + 5:
            state = "running"
        elif rec.get("finished") and not rec.get("ok"):
            state = "error"
        elif age is None:
            state = "never"
        elif age > job.late:
            state = "late"
        else:
            state = "ok"
        out.append({"key": job.key, "title": job.title, "every": job.every, "state": state, "last_ok": last_ok,
                    "age": age, "finished": rec.get("finished"), "message": rec.get("message", ""),
                    "output": rec.get("output", ""),
                    "registered": registered.get(job.key) if registered is not None else None})
    return out


def scheduled() -> dict[str, bool] | None:
    """Какие задачи есть в Планировщике заданий Windows; None — не Windows или schtasks недоступен."""
    if sys.platform != "win32":
        return None
    found = {}
    for key in JOBS:
        try:
            found[key] = subprocess.run(["schtasks", "/query", "/tn", TASK_PREFIX + key], capture_output=True,
                                        timeout=10, creationflags=NO_WINDOW).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return None
    return found


def ago(minutes: float | None) -> str:
    if minutes is None:
        return "ни разу"
    if minutes < 2:
        return "только что"
    if minutes < 90:
        return f"{round(minutes)} мин назад"
    if minutes < 36 * 60:
        return f"{round(minutes / 60)} ч назад"
    return f"{round(minutes / 60 / 24)} дн назад"


def every(minutes: int) -> str:
    if minutes % (24 * 60) == 0:
        return "раз в день" if minutes == 24 * 60 else f"раз в {minutes // (24 * 60)} дн"
    return "каждый час" if minutes == 60 else f"каждые {minutes} мин"


def warnings(rows: list[dict]) -> list[str]:
    """Что сказать администратору: настроенная задача не работает или давно не было резервной копии."""
    out = []
    for r in rows:
        if r["state"] == "error":
            out.append(f"{r['title']}: последний запуск с ошибкой — {r['message'] or 'см. вывод'}")
        elif r["state"] == "late":
            out.append(f"{r['title']}: последний успешный запуск {ago(r['age'])} (должна — {every(r['every'])})")
        elif r["state"] == "never" and r["key"] == "backup":
            out.append("Резервной копии ещё нет — создайте её здесь же или зарегистрируйте задачи "
                       "(scripts\\install_tasks.bat)")
    from . import backup
    if backup.copy_dir():
        last = backup.last_copy()
        if last and not last["ok"]:
            out.append(f"Копия резервного архива на «{last['target']}» не записана: {last['error']}")
        elif last and last.get("same_disk"):
            out.append(f"Копия резервного архива лежит на том же диске, что и система ({last['target']}) — "
                       "при отказе диска пропадут обе; укажите папку на другом диске или в сети")
    return out

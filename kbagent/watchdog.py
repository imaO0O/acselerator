"""Сторож: модели, чат и REST API запускаются сами при входе в Windows и перезапускаются, если упали.

Без него после перезагрузки сервера всё запускается вручную (start_servers.bat, run_chat.bat в открытых окнах), а
упавший сервер модели молча останавливает ответы. scripts/install_tasks.bat регистрирует задачу KBAgent-watchdog:
при входе и каждые 2 минуты — один проход scripts/watchdog.py: что не отвечает, то запускается (модели —
start_servers.ps1 в режиме KB_MODEL_MODE, чат и API — в фоне, без окон). Если служба после перезапуска так и не
поднялась, следующий перезапуск — не раньше чем через 10 минут, чтобы не крутиться впустую; во вкладке «Система» —
сколько каждая служба работает без сбоев, сколько было перезапусков и последняя ошибка (logs/watchdog.json).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, settings

SCRIPTS = ROOT / "scripts"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DETACHED = 0x00000008 if sys.platform == "win32" else 0
RETRY_MINUTES = 10                  # не поднялась после перезапуска — следующая попытка не раньше
CHAT_PORT, API_PORT = 8501, 8000
TITLES = {"models": "Модели (GigaChat, поиск, переранжирование)", "chat": "Чат", "api": "REST API"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _path() -> Path:
    return settings.log_dir / "watchdog.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"services": {}}


def _save(state: dict) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_path())


def watched() -> list[str]:
    """Модели — если llama.cpp стоит здесь (а не внешний сервер моделей); API — если включён KB_API_AUTOSTART."""
    out = []
    local = all("127.0.0.1" in u or "localhost" in u for u in (settings.llm_url, settings.embed_url,
                                                                  settings.rerank_url))
    if local and (ROOT / "tools" / "llama.cpp" / "llama-server.exe").exists():
        out.append("models")
    out.append("chat")
    if os.environ.get("KB_API_AUTOSTART", "").strip() in ("1", "true", "да"):
        out.append("api")
    return out


def _http_ok(port: int, path: str) -> bool:
    import requests
    import urllib3
    from . import tls
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    scheme = "https" if tls.paths() else "http"
    try:
        # свой сертификат на localhost проверять незачем: запрос не уходит с этого компьютера
        return requests.get(f"{scheme}://127.0.0.1:{port}{path}", timeout=5, verify=False).status_code == 200
    except requests.RequestException:
        return False


def check(names: list[str]) -> dict[str, bool]:
    from . import llm
    out = {}
    for name in names:
        if name == "models":
            out[name] = all(llm.health().values())
        elif name == "chat":
            out[name] = _http_ok(CHAT_PORT, "/_stcore/health")
        elif name == "api":
            out[name] = _http_ok(API_PORT, "/health") or _http_ok(API_PORT, "/docs")
    return out


def _python() -> str:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless) if windowless.exists() else str(exe)


def _background(args: list[str], log_name: str) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.log_dir / log_name, "ab") as log:
        subprocess.Popen([_python(), *args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW | DETACHED, close_fds=True)


def start(name: str) -> str:
    """Запуск службы; пусто — запущена, иначе — что пошло не так."""
    if name == "models":
        mode = {"cpu": ["-Cpu"], "smallgpu": ["-SmallGpu"]}.get(os.environ.get("KB_MODEL_MODE", "").lower(), [])
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(SCRIPTS / "start_servers.ps1"), *mode], cwd=ROOT, capture_output=True,
                               timeout=300, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"start_servers не выполнился: {e}"
        tail = r.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        return "" if r.returncode == 0 else f"start_servers: {tail[0][:200]} (logs\\llm.log)"
    if name == "chat":
        _background([str(SCRIPTS / "run_chat.py"), "--server.headless", "true"], "chat.log")
    elif name == "api":
        _background([str(SCRIPTS / "run_api.py"), "--host", os.environ.get("KB_API_HOST", "127.0.0.1")], "api.log")
    return ""


def _minutes_since(stamp: str | None, now: float) -> float:
    if not stamp:
        return 1e9
    return (now - time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))) / 60


def run_once(names: list[str] | None = None, wait: float = 60, checker=check, starter=start) -> dict:
    """Один проход сторожа: проверить, перезапустить упавшее, записать. wait — сколько ждать, пока чат и API
    поднимутся после запуска (модели ждёт сам start_servers)."""
    names = names or watched()
    state = load()
    services = state.setdefault("services", {})
    now = time.time()
    up = checker(names)
    started = []
    for name in names:
        s = services.setdefault(name, {"restarts": 0})
        if up[name]:
            if not s.get("up"):
                s["since"] = _now()
            s.update(up=True, failing=0)
            continue
        if s.get("up"):
            s["down_at"] = _now()
        s["up"] = False
        if s.get("failing", 0) and _minutes_since(s.get("last_restart"), now) < RETRY_MINUTES:
            continue                                     # только что пробовали — не крутимся впустую
        s.update(last_restart=_now(), restarts=s.get("restarts", 0) + 1)
        s["history"] = (s.get("history", []) + [_now()])[-50:]
        error = starter(name)
        if error:
            s.update(last_error=error, failing=s.get("failing", 0) + 1)
        else:
            started.append(name)
    deadline = time.time() + wait
    while started and time.time() < deadline:
        time.sleep(2)
        now_up = checker(started)
        for name in [n for n in started if now_up[n]]:
            services[name].update(up=True, since=_now(), failing=0, last_error="")
            started.remove(name)
    for name in started:                                 # запущена, но так и не ответила
        services[name].update(failing=services[name].get("failing", 0) + 1,
                              last_error=services[name].get("last_error") or "запущена, но не отвечает")
    state["last_check"] = _now()
    _save(state)
    return state


def summary(state: dict | None = None, now: float | None = None) -> list[dict]:
    """По каждой службе: работает ли, сколько без сбоев, перезапусков за 7 дней, последняя ошибка."""
    state = state or load()
    now = now or time.time()
    week = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 7 * 86400))
    out = []
    for name, s in state.get("services", {}).items():
        out.append({"name": name, "title": TITLES.get(name, name), "up": bool(s.get("up")),
                    "uptime": _minutes_since(s.get("since"), now) if s.get("up") else None,
                    "restarts_week": sum(1 for t in s.get("history", []) if t >= week),
                    "failing": s.get("failing", 0), "error": s.get("last_error", ""),
                    "down_at": s.get("down_at")})
    return out

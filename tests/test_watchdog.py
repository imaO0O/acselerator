"""Сторож: перезапуск упавшего, пауза между безуспешными попытками, сводка для вкладки «Система»."""
import time

from kbagent import jobs, watchdog


def test_restarts_what_is_down_and_records_it(tmp_settings):
    up = {"models": True, "chat": False}
    started = []

    def starter(name):
        started.append(name)
        up[name] = True                   # поднялся после запуска
        return ""

    state = watchdog.run_once(["models", "chat"], wait=5, checker=lambda names: {n: up[n] for n in names},
                              starter=starter)
    assert started == ["chat"]
    chat = state["services"]["chat"]
    assert chat["up"] and chat["restarts"] == 1 and chat["failing"] == 0 and state["services"]["models"]["up"]
    # всё работает — следующий проход ничего не трогает
    watchdog.run_once(["models", "chat"], wait=0, checker=lambda names: {n: True for n in names}, starter=starter)
    assert started == ["chat"]
    rows = {r["name"]: r for r in watchdog.summary()}
    assert rows["chat"]["up"] and rows["chat"]["restarts_week"] == 1 and rows["chat"]["uptime"] < 1


def test_failed_start_is_not_retried_every_two_minutes(tmp_settings, monkeypatch):
    calls = []
    down = lambda names: {n: False for n in names}  # noqa: E731
    starter = lambda name: calls.append(name) or "start_servers: llama-server не найден"  # noqa: E731
    watchdog.run_once(["models"], wait=0, checker=down, starter=starter)
    watchdog.run_once(["models"], wait=0, checker=down, starter=starter)          # через 2 минуты — пауза
    assert calls == ["models"]
    s = watchdog.load()["services"]["models"]
    assert s["failing"] == 1 and "llama-server" in s["last_error"] and not s["up"]
    later = time.time() + 11 * 60
    monkeypatch.setattr(watchdog.time, "time", lambda: later)
    watchdog.run_once(["models"], wait=0, checker=down, starter=starter)          # через 10 минут — снова
    assert calls == ["models", "models"] and watchdog.load()["services"]["models"]["failing"] == 2


def test_started_but_silent_counts_as_failure(tmp_settings):
    watchdog.run_once(["chat"], wait=3, checker=lambda names: {n: False for n in names}, starter=lambda n: "")
    row = watchdog.summary()[0]
    assert not row["up"] and row["failing"] == 1 and row["error"] == "запущена, но не отвечает"


def test_watchdog_runs_at_logon_and_every_two_minutes():
    job = jobs.JOBS["watchdog"]
    assert job.logon and job.every == 2 and (jobs.SCRIPTS / job.script).exists()
    assert "chat" in watchdog.watched()

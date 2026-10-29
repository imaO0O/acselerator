"""Обновление одной командой: данные — из прежней установки в новую; при сбое прежняя запускается снова."""
import pytest

from kbagent import backup, update


def _install(root, users="users: []\n"):
    (root / "kbagent").mkdir(parents=True)
    (root / "data" / "raw" / "uploaded").mkdir(parents=True)
    (root / "data" / "users.yaml").write_text(users, encoding="utf-8")
    return root


class Ops:
    def __init__(self):
        self.calls = []

    def ops(self):
        return update.Ops(stop=lambda r: self.calls.append(("stop", r.name)),
                          tasks=lambda r, on: self.calls.append(("tasks", r.name, on)),
                          start=lambda r: self.calls.append(("start", r.name)))


def test_data_moves_to_new_install(tmp_path, tmp_settings, monkeypatch):
    monkeypatch.setattr(backup, "running", lambda: [])
    old = _install(tmp_path / "old", "users:\n- login: ivanov\n")
    (old / "data" / "raw" / "uploaded" / "STP-05.pdf").write_bytes(b"%PDF-1.4")
    (old / "data" / "settings.env").write_text("KB_DOCS_FOLDER=D:/docs\n", encoding="utf-8")
    (old / "logs").mkdir()
    (old / "logs" / "actions.jsonl").write_text('{"action": "login"}\n', encoding="utf-8")
    new = _install(tmp_path / "new", "users:\n- login: admin\n")
    o = Ops()
    rep = update.migrate(old, new, o.ops(), log=lambda m: None)
    assert (new / "data" / "users.yaml").read_text(encoding="utf-8") == "users:\n- login: ivanov\n"
    assert (new / "data" / "raw" / "uploaded" / "STP-05.pdf").exists() and (new / "data" / "settings.env").exists()
    assert (new / "logs" / "actions.jsonl").exists() and rep["safety"].exists()
    assert o.calls == [("tasks", "old", False), ("stop", "old"), ("tasks", "new", True), ("start", "new")]
    assert (old / "data" / "users.yaml").read_text(encoding="utf-8") == "users:\n- login: ivanov\n"  # не тронута


def test_failure_brings_old_install_back(tmp_path, tmp_settings, monkeypatch):
    monkeypatch.setattr(backup, "running", lambda: [])
    old, new = _install(tmp_path / "old"), _install(tmp_path / "new")

    def broken(*args, **kwargs):
        raise OSError("диск переполнен")

    monkeypatch.setattr(backup, "restore", broken)
    o = Ops()
    with pytest.raises(update.UpdateError, match="прежняя установка запущена"):
        update.migrate(old, new, o.ops(), log=lambda m: None)
    assert o.calls[-2:] == [("tasks", "old", True), ("start", "old")]


def test_refuses_wrong_folder_and_running_services(tmp_path, tmp_settings, monkeypatch):
    new = _install(tmp_path / "new")
    with pytest.raises(update.UpdateError, match="нет установки"):
        update.migrate(tmp_path / "пусто", new, Ops().ops(), log=lambda m: None)
    with pytest.raises(update.UpdateError, match="не эту"):
        update.migrate(new, new, Ops().ops(), log=lambda m: None)
    old = _install(tmp_path / "old")
    monkeypatch.setattr(backup, "running", lambda: ["чат (порт 8501)"])
    o = Ops()
    with pytest.raises(update.UpdateError, match="не остановились"):
        update.migrate(old, new, o.ops(), log=lambda m: None)
    assert o.calls[-1] == ("tasks", "old", True)            # задачи вернулись на прежнюю

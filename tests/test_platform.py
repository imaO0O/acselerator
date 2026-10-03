"""Вход, сеансы, диалоги, демо-база, каталог редакций, маршрутизация, оценка, API."""
from datetime import date

from kbagent import auth, demo_db
from kbagent.agent import Agent
from kbagent.ingest import ARCHIVE, access_of, is_cancelled


def test_password_hash_roundtrip():
    stored = auth.hash_password("тест-пароль")
    assert auth.verify_password("тест-пароль", stored) and not auth.verify_password("другой", stored)


def test_sessions_and_conversations(tmp_settings, monkeypatch):
    monkeypatch.setattr(auth, "load_users", lambda: {"u": {"login": "u", "name": "У", "role": "Метролог"}})
    token = auth.create_session({"login": "u"})
    assert auth.session_user(token)["role"] == "Метролог"
    auth.end_session(token)
    assert auth.session_user(token) is None

    conv = auth.new_conversation()
    conv["items"].append({"question": "Какой срок входного контроля?", "text": "5 дней [1]"})
    auth.save_conversation("u", conv)
    listed = auth.list_conversations("u")
    assert listed[0]["title"] == "Какой срок входного контроля?"
    auth.delete_conversation("u", conv["id"])
    assert auth.list_conversations("u") == []


def test_demo_dates_shift_with_today():
    delta = demo_db.offset(date(2026, 12, 4))
    assert demo_db.shift("2026-09-25", delta) == "2026-11-26"
    assert demo_db.shift("2026-09-30 02:45", delta) == "2026-12-01 02:45"
    assert demo_db.shift("ОБ-0101", delta) == "ОБ-0101"


def test_cancelled_edition_is_archived():
    assert is_cancelled({"status": "отменён"}) and access_of({"status": "отменён", "access": "all"}) == ARCHIVE
    assert access_of({"access": "metrology"}) == "metrology"


def test_routing_rules_without_model():
    agent = Agent.__new__(Agent)          # без загрузки индекса: проверяются правила до обращения к модели
    assert agent.route("Можно ли выдать в производство партию П-2609-04?") == "both"
    assert agent.route("Что делать при искрении оборудования?") == "documents"
    assert agent.route("В какой срок приступит ремонтная служба?") == "documents"
    assert agent.route("Когда поверка СИ-0018?") == "database"
    assert agent.route("Сколько стоит поверка одного осциллографа?") == "documents"
    assert agent.route("скока дней длится входной контроль эри") == "documents"


def test_eval_helpers():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).parents[1] / "eval" / "run_eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    assert ev.clause_covers("4.1–4.3", "4.2") and ev.clause_covers("3.1–3.4", "3") and not ev.clause_covers("5.1", "4.2")
    assert "12.11.2026" in ev.expand_dates("date:2026-11-12", demo_db.offset(date(2026, 10, 3)))
    assert ev.has_facts("Срок — 5 рабочих дней", ["5 рабочих дней"]) and ev.has_none("2211", ["2200"])


def test_api_requires_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api import server
    monkeypatch.setattr(server, "KEYS_PATH", tmp_path / "keys.yaml")
    (tmp_path / "keys.yaml").write_text(
        f"keys:\n- name: t\n  role: Метролог\n  hash: {server.key_hash('kb_good')}\n", encoding="utf-8")
    api = TestClient(server.app)
    assert api.post("/ask", json={"question": "тест вопроса"}).status_code == 401
    assert api.post("/ask", json={"question": "тест вопроса"}, headers={"X-API-Key": "bad"}).status_code == 401
    assert server.client("kb_good")["role"] == "Метролог"

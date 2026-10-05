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


def test_api_requires_key(tmp_path, monkeypatch, tmp_settings):
    from fastapi.testclient import TestClient

    from api import server
    monkeypatch.setattr(server, "KEYS_PATH", tmp_path / "keys.yaml")
    (tmp_path / "keys.yaml").write_text(
        f"keys:\n- name: t\n  role: Метролог\n  hash: {server.key_hash('kb_good')}\n", encoding="utf-8")
    api = TestClient(server.app)
    assert api.post("/ask", json={"question": "тест вопроса"}).status_code == 401
    assert api.post("/ask", json={"question": "тест вопроса"}, headers={"X-API-Key": "bad"}).status_code == 401
    assert server.client("kb_good")["role"] == "Метролог"


def test_named_documents_and_rule_routing():
    from kbagent.agent import named_docs
    cards = {"g": {"title": "ГОСТ Р 7.0.97-2016 Организационно-распорядительная документация"},
             "s": {"title": "СТП ДП-01-2026 «Порядок входного контроля»"},
             "o": {"title": "Осциллографы цифровые АКИП-4115/1В, АКИП-4115/2В"},
             "m": {"title": "Мультиметр цифровой MASTECH MAS830L"}}
    assert named_docs("какие поля у документа по госту 7.0.97", cards) == {"g"}
    assert named_docs("Что в пункте 3.2 СТП ДП-01-2026?", cards) == {"s"}
    assert named_docs("Предохранитель в MAS830L?", cards) == {"m"}
    assert named_docs("Какой срок входного контроля?", cards) == set()
    agent = Agent.__new__(Agent)
    assert agent.route("Пришла партия 450 резисторов. Сколько штук нужно осмотреть?") == "documents"


def test_source_preview_marks_whole_fragment_across_lines(tmp_path):
    import pymupdf
    from kbagent import preview
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "2.1 Equipment failures are divi-")
    page.insert_text((72, 90), "ded into three categories. Other text below.")
    path = tmp_path / "d.pdf"
    doc.save(path)
    png, marked = preview.render_page(str(path), 1, "2.1 Equipment failures are divided into three categories.",
                                      crop=True)
    assert marked == 9 and png[:4] == b"\x89PNG"     # перенос «divi-/ded» склеен, «Other text below» не выделен


def test_continuation_of_long_clause_gets_its_own_pages():
    from kbagent import ingest
    sentence = "Работник выполняет требования инструкции по охране труда при работе на оборудовании участка. "
    pages = [(1, ["1 Общие положения", "1.1 " + sentence * 6]), (2, [sentence * 6]), (3, [sentence * 6])]
    meta = {"id": "doc", "title": "Документ", "kind": "Инструкция", "access": "all"}
    chunks = ingest._chunks(ingest._units(pages), meta)
    assert len(chunks) >= 3 and chunks[0].page_start == 1
    assert chunks[-1].page_start >= 2 and chunks[-1].page_end == 3     # не «стр. 1–3» у последнего куска


def test_uploaded_document_title_is_taken_from_its_text(tmp_path):
    from docx import Document
    from kbagent.uploads import guess_title
    doc = Document()
    for line in ["ООО «Демо-Прибор»", "РИ ДП-05-2026", "ОБСЛУЖИВАНИЕ УСТАНОВКИ СЕЛЕКТИВНОЙ ПАЙКИ", "1.1 Текст пункта."]:
        doc.add_paragraph(line)
    path = tmp_path / "RI-DP-05.docx"
    doc.save(path)
    assert guess_title(path) == "РИ ДП-05-2026 «Обслуживание установки селективной пайки»"


def test_unanswered_questions_are_grouped_into_topics():
    from kbagent.gaps import find_gaps
    rec = lambda q, status, user="petrov", t="2026-10-05 10:00:00": {  # noqa: E731
        "time": t, "user": user, "role": "Инженер производства", "question": q, "status": status, "route": "documents"}
    records = [rec("Сколько стоит поверка осциллографа?", "no_data"),
               rec("Какая стоимость поверки осциллографа?", "no_data", user="ivanov", t="2026-10-05 11:00:00"),
               rec("Где взять пропуск на склад?", "model_refused"),
               rec("Что делать при искрении?", "answer"),
               rec("Сколько стоит поверка осциллографа?", "no_data", user="eval")]       # прогон эталона — не в счёт
    topics, total = find_gaps(records)
    assert total == {"questions": 4, "unanswered": 3, "topics": 2}
    assert topics[0]["count"] == 2 and topics[0]["users"] == 2 and topics[0]["last"] == "2026-10-05 11:00:00"


def test_actions_journal_records_who_did_what(tmp_settings):
    from kbagent.actions import load_actions, log_action
    log_action("login_failed", "ivanov")
    log_action("upload", "admin", "РИ ДП-05-2026", "фрагментов: 7")
    records = load_actions()
    assert [r["action"] for r in records] == ["login_failed", "upload"]
    assert records[1]["user"] == "admin" and records[1]["target"] == "РИ ДП-05-2026"

"""Ответ на дату: разбор даты в вопросе, какие редакции тогда действовали, отбор документов для поиска."""
import datetime as dt

from kbagent import asof
from kbagent.agent import scoped

TODAY = dt.date(2026, 10, 29)
# демо-реестр: СТП ДП-02 2024 года заменён редакцией 2026-го с 01.03.2026; приказ действовал до конца 2025-го
CATALOG = {
    "stp02_2024": {"title": "СТП ДП-02-2024", "access": "all", "effective_from": "2024-01-15", "status": "отменён",
                   "replaced_by": "stp02_2026"},
    "stp02_2026": {"title": "СТП ДП-02-2026", "access": "all", "effective_from": "2026-03-01"},
    "ri04": {"title": "РИ ДП-04-2026", "access": "metrology", "effective_from": "2026-03-01"},
    "prikaz903": {"title": "Приказ 903н", "access": "all", "effective_from": "2021-01-01", "effective_to": "2025-12-31"},
    "draft": {"title": "СТП ДП-02 (проект)", "access": "all", "replaces": "stp02_2026", "approved": False},
    "lost": {"title": "Старый регламент", "access": "all", "status": "отменён"},
}
DOCS = set(CATALOG) | {"gost_r_2_105"}          # ГОСТ — без записи в реестре: сроков не знаем


def test_dates_in_questions():
    p = lambda q: asof.parse(q, TODAY)  # noqa: E731
    assert p("Какой срок поверки действовал на 01.03.2025?") == asof.Window("2025-03-01", "2025-03-01",
                                                                             "на 01.03.2025")
    assert p("что делали при отказе оборудования 5 июня 2025 года").start == "2025-06-05"
    assert p("Как оформляли отказ в марте 2025 года?") == asof.Window("2025-03-01", "2025-03-31",
                                                                       "в марте 2025 года")
    assert p("по какой редакции работали в мае 2025").end == "2025-05-31"
    assert p("какой порядок был в 2024 году") == asof.Window("2024-01-01", "2024-12-31", "в 2024 году")
    assert p("что требовалось в прошлом году").label == "в 2025 году"
    assert p("а год назад как было?").start == "2025-10-29"
    # текущий год — окно до сегодняшнего дня
    assert p("какие требования были в 2026 году").end == "2026-10-29"


def test_not_a_date():
    for q in ["Поля документа по ГОСТ Р 7.0.97-2016", "что сказано в п. 4.2.10", "ГОСТ Р 2.105-2019 о чём?",
              "Сколько действует наряд-допуск?", "что будет действовать с 01.12.2026",
              "срок поверки на 29.10.2026", "партия 2025 года на складе"]:
        assert asof.parse(q, TODAY) is None, q


def test_which_editions_were_in_force():
    assert asof.period("stp02_2024", CATALOG) == ("2024-01-15", "2026-02-28")
    assert asof.period("prikaz903", CATALOG) == ("2021-01-01", "2025-12-31")
    assert asof.period("lost", CATALOG) is None            # отменён, а когда — неизвестно
    s = lambda q, allowed={"all"}: asof.scope(asof.parse(q, TODAY), allowed, DOCS, CATALOG)  # noqa: E731
    assert s("на 01.06.2025") == {"stp02_2024", "prikaz903", "gost_r_2_105"}
    assert s("на 01.06.2026") == {"stp02_2026", "gost_r_2_105"}
    # в 2026 году действовали обе редакции; метрологу виден и его РИ, проект редакции — никому
    assert s("в 2026 году", {"all", "metrology"}) == {"stp02_2024", "stp02_2026", "ri04", "gost_r_2_105"}
    assert asof.current_edition("stp02_2024", CATALOG) == "stp02_2026"


def test_note_says_what_was_and_what_is():
    w = asof.parse("на 01.06.2025", TODAY)
    text = asof.note(w, ["stp02_2024", "stp02_2024"], CATALOG)
    assert text == ("**На 01.06.2025** действовал: «СТП ДП-02-2024» (с 15.01.2024 по 28.02.2026); "
                    "сейчас действует «СТП ДП-02-2026».")
    same = asof.note(asof.parse("на 01.06.2026", TODAY), ["stp02_2026"], CATALOG)
    assert same.startswith("**На 01.06.2026** действовали те же редакции, что и сейчас: «СТП ДП-02-2026» (с 01.03.2026")
    assert asof.note(w, [], CATALOG) == ""
    # внутри 2026 года редакция сменилась 1 марта — названы обе
    year = asof.note(asof.parse("в 2026 году", TODAY), ["stp02_2026"], CATALOG)
    assert year == ("**В 2026 году** действовал: «СТП ДП-02-2026» (с 01.03.2026 — действует и сейчас); в этот же "
                    "период действовал и «СТП ДП-02-2024» (с 15.01.2024 по 28.02.2026).")


def test_scoped_search_keeps_to_editions_of_that_time(monkeypatch):
    from kbagent import agent as G
    calls = []
    monkeypatch.setattr(G, "retrieve", lambda index, q, allowed, docs=None: calls.append((allowed, docs)) or "found")
    assert scoped(None, "q", {"all"}, None, None) == "found" and calls[-1] == ({"all"}, None)
    scoped(None, "q", {"all", "archive"}, {"stp02_2024", "stp02_2026"}, {"stp02_2024", "prikaz903"})
    assert calls[-1][1] == {"stp02_2024"}          # названный документ — среди действовавших тогда
    scoped(None, "q", {"all"}, {"ri04"}, {"stp02_2024"})
    assert calls[-1][1] == {"stp02_2024"}          # названного тогда не было — ищем по действовавшим
    empty = scoped(None, "q", {"all"}, None, set())
    assert empty.hits == [] and not empty.confident and len(calls) == 3


def test_catalog_keeps_old_edition_until_new_takes_effect(tmp_path):
    import time
    import yaml
    from kbagent.ingest import ARCHIVE, DRAFT, access_of, load_catalog
    raw = tmp_path / "raw"
    (raw / "uploaded").mkdir(parents=True)
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump({"documents": [{"id": "stp_02", "title": "СТП ДП-02"}]}),
                                           encoding="utf-8")
    future = time.strftime("%Y") + "9-01-01"
    new = {"id": "stp_02_new", "title": "СТП ДП-02 (нов.)", "replaces": "stp_02", "approved": True,
           "effective_from": future}
    (raw / "uploaded" / "catalog.yaml").write_text(yaml.safe_dump({"documents": [new]}), encoding="utf-8")
    cat = load_catalog(raw)
    # утверждена заранее: до даты вступления сотрудники работают по прежней, новую видит администратор
    assert access_of(cat["stp_02"]) == "all" and access_of(cat["stp_02_new"]) == DRAFT
    new["effective_from"] = "2026-01-01"
    (raw / "uploaded" / "catalog.yaml").write_text(yaml.safe_dump({"documents": [new]}), encoding="utf-8")
    cat = load_catalog(raw)
    assert access_of(cat["stp_02"]) == ARCHIVE and access_of(cat["stp_02_new"]) == "all"

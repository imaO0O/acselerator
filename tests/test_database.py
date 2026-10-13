"""Учётная база: безопасность запросов, исправления SQL, проверка чисел, похожие записи."""
import pytest

from kbagent import database as D
from kbagent import demo_db


@pytest.fixture(scope="module")
def db():
    return demo_db.build()


def test_only_select_is_allowed():
    D._check("SELECT * FROM stock")
    for bad in ["DELETE FROM stock", "SELECT 1; DROP TABLE stock", "UPDATE stock SET qty = 0"]:
        with pytest.raises(ValueError):
            D._check(bad)


def test_connection_is_read_only(db):
    with pytest.raises(Exception):
        D._run(db, "INSERT INTO stock (part_no) VALUES ('X')")


def test_dates_are_local_and_compared_by_day():
    sql = D._fix_dates("SELECT * FROM r WHERE r.created_at BETWEEN date('now','-3 days') AND date('now')")
    assert "date(r.created_at)" in sql and "'localtime'" in sql
    assert D._fix_dates("WHERE next_check <= date('end_of_year')").endswith("end_of_year()")


def test_fuzzy_names_and_people():
    assert D._fuzzy_text("WHERE e.responsible = 'Белов'") == "WHERE lower(e.responsible) LIKE '%белов%'"
    assert D._fuzzy_text("WHERE status = 'в ремонте'") == "WHERE status = 'в ремонте'"


def test_like_ignores_case_and_yo():
    assert D._like("%разъем db-9%", "Разъём DB-9 розетка на плату")


def test_alias_repair():
    assert D._repair_aliases("SELECT e.name FROM equipment WHERE e.status = 'x'").startswith(
        "SELECT e.name FROM equipment e")


def test_numbers_must_come_from_records():
    rows = [("ОБ-0102", "Автомат", "2026-09-28 14:20")]
    assert not D._numbers_supported("ОБ-0102, срок ремонта 14 дней [БД]", rows, "В какой срок?")
    assert D._numbers_supported("ОБ-0102, подана 28.09.2026 [БД]", rows, "Когда подана заявка?")


def test_labels_are_russian():
    assert D.column_label("next_check").startswith("дата очередной поверки")
    assert D._labels("SELECT * FROM stock", ["received", "location"]) == ["дата поступления", "место хранения"]


def test_near_matches_find_item_in_other_table(db):
    found = D._near_matches(db, "Где лежит флюс?")
    assert found and found[0][3]["name"] == "stock"


def test_near_match_ignores_word_middles(db):
    # «контрол» внутри «Микроконтроллер» — не ответ на вопрос о входном контроле
    assert D._answer_near("скока дней длится входной контроль эри", db, {}) is None
    exact = D._answer_near("Где лежит флюс?", db, {})
    similar = D._answer_near("Сколько осталось припоя ПОС-40?", db, {})
    assert exact.text.startswith("Найдено") and similar.text.startswith("Точного совпадения в учёте нет")


def test_record_lookup_and_name_mismatch(db):
    a = D.answer_from_database("У мультиметра СИ-0012 истёк срок?", db, records=["СИ-0012"])
    assert a.status == "answer" and "Осциллограф" in a.text and "в вопросе" in a.text


def test_schema_description_is_wrapped():
    assert all(len(line) <= 200 for line in D.SCHEMA_DESCRIPTION.splitlines())


def test_retelling_must_name_found_records():
    cols, labels = ["inv_no", "name", "status"], ["инвентарный номер", "наименование", "состояние"]
    rows = [("ОБ-0302", "Вибростенд", "законсервировано")]
    assert D._retelling_problem("Законсервация: Нет.", cols, labels, rows)        # запись не названа
    assert D._retelling_problem("Записей нет.", cols, labels, rows) == "записей нет"
    assert D._retelling_problem("ОБ-0302 | Вибростенд", cols, labels, rows) == "таблица"
    assert D._retelling_problem("Законсервирован вибростенд ОБ-0302.", cols, labels, rows) is None


def test_plain_answer_from_records():
    cols = ["inv_no", "name", "qty", "unit", "next_check", "responsible"]
    labels = ["номер", "наименование", "количество", "единица", "дата очередной поверки", "ответственный"]
    rows = [("А-1", "Припой", 12.0, "кг", "2026-11-13", "Белов И. Н."),
            ("А-2", "Флюс", 8, "л", "2026-12-01", "Белов И. Н.")]
    text = D._plain_answer(cols, labels, rows, "Когда очередная поверка?")
    assert text.startswith("Найдено записей: 2 (ответственный — Белов И. Н.).")
    assert "- А-1 — Припой: дата очередной поверки — 13.11.2026; количество — 12 кг" in text


def test_labels_follow_table_aliases():
    sql = ("SELECT e.inv_no, r.id AS repair_id, r.status FROM equipment e "
           "JOIN repair_requests r ON e.inv_no = r.inv_no")
    assert D._labels(sql, ["inv_no", "repair_id", "status"]) == ["инвентарный номер", "номер заявки", "статус заявки"]


def test_count_with_plain_columns_needs_group_by():
    with pytest.raises(ValueError):
        D._check("SELECT e.inv_no, e.name, COUNT(*) AS \"всего\" FROM equipment e WHERE e.status = 'в работе'")
    D._check("SELECT COUNT(*) AS \"всего\" FROM equipment WHERE status = 'в работе'")
    D._check("SELECT kind, COUNT(*) FROM equipment GROUP BY kind")


def test_price_questions_do_not_go_to_records(db):
    r = D.answer_from_database("А сколько стоит обед в столовой предприятия?", db)
    assert r.status == "no_data" and not r.sql       # без вызова модели и без запроса к базе


def test_total_is_counted_by_program_for_how_many_in_total():
    cols = ["part_no", "name", "qty", "unit"]
    rows = [["ЭРИ-1001", "Резистор 10 кОм", 4000, "шт"], ["ЭРИ-1002", "Резистор 1 кОм", 2500, "шт"]]
    assert D._total_line("Сколько всего резисторов на складе?", cols, rows) == "Всего: 6500 шт (записей: 2)."
    assert D._total_line("Где лежат резисторы?", cols, rows) == ""                      # не спрашивали сумму
    assert D._total_line("Сколько всего?", cols, [rows[0], ["ЭРИ-9", "Припой", 12, "кг"]]) == ""   # разные единицы

"""Правила из критической проверки 26.10 (eval/svezhie.yaml): маршруты, цена из прайса, счётчик учёта,
опора ответа на фрагменты, разбор писем. Без моделей."""
from conftest import make_hit

from kbagent import answer as A
from kbagent import tkp
from kbagent.agent import RULE_RE, SERVICE_RE, TOPIC_ONLY_RE, Agent
from kbagent.database import _count_answer

P52 = ("52. В объём периодической проверки электроинструмента и приспособлений входят: внешний осмотр; проверка "
       "работы на холостом ходу в течение не менее 5 минут; измерение сопротивления изоляции мегаомметром на "
       "напряжение 500 В в течение 1 минуты при выключателе в положении \"вкл\", при этом сопротивление изоляции "
       "должно быть не менее 0,5 Мом (за исключением аккумуляторного инструмента); проверка исправности цепи "
       "заземления (для электроинструмента класса I).")


def test_routes_for_duration_topic_only_and_services():
    assert RULE_RE.search("Сколько хранятся записи входного контроля?")        # срок по регламенту, не счётчик
    assert RULE_RE.search("Как долго хранится журнал?")
    assert TOPIC_ONLY_RE.match("Расскажи про поверку") and TOPIC_ONLY_RE.match("что известно о браке?")
    assert not TOPIC_ONLY_RE.match("Расскажи, как отправить осциллограф на поверку")
    assert SERVICE_RE.search("Сколько стоит калибровка мультиметра MAS830L?")
    assert not SERVICE_RE.search("Сколько стоит АКИП-4143/1?")


def test_price_and_stock_come_from_price_list():
    r = Agent.answer_price(None, "Сколько стоит АКИП-4143/1?", "Сколько стоит АКИП-4143/1?", "Техпродажи и сервис")
    assert r.route == "prajs" and r.status == "answer"
    assert "АКИП-4143/1** — 189 000 ₽" in r.text and "ДЕМО" in r.text and "подтверждает менеджер" in r.text
    assert Agent.answer_price(None, "Сколько стоит поверка осциллографа?", "Сколько стоит поверка осциллографа?",
                              "Техпродажи и сервис") is None                     # модель не названа — не прайс


def test_count_from_database_is_said_by_program():
    sql = "SELECT COUNT(*) AS \"всего\" FROM repair_requests WHERE status IN ('новая', 'в работе')"
    assert _count_answer(sql, ["всего"], [(3,)]) == "Заявки на ремонт оборудования (новая, в работе): 3"
    assert _count_answer("SELECT name FROM equipment", ["name"], [("Печь",)]) is None
    assert _count_answer("SELECT COUNT(*) FROM stock WHERE name LIKE '%резист%'", ["n"], [(4,)]) == \
        "Остатки на складе: 4"


def test_answer_terms_and_values_must_come_from_fragments():
    hits = [make_hit("3. Изменение параметров. Нажмите кнопку DISPLAY для вызова меню дисплея.", "osc", "3"),
            make_hit("Acquire/СборИнф>XY Mode/XY режим", "osc", None)]
    assert A._unsupported_terms("Как включить XY?", "Нажмите DISPLAY и выберите XY Mode [1].", hits, [1]) == ["Mode"]
    assert A._unsupported_terms("Как включить XY?", "Меню Acquire → XY Mode [2].", hits, [2]) == []
    assert A._unsupported_terms("Какой класс?", "Класс II [1].", hits, [1]) == []          # римские цифры — не термины
    assert A.VALUE_Q_RE.search("Какое сопротивление изоляции должно быть у электроинструмента класса II?")
    assert not A.VALUE_Q_RE.search("Какое оборудование в ремонте?")
    assert not A._has_number("Электроинструмент класса II не заземляется [1].")
    # требование для всех классов не выбрасывается из-за оговорки в скобках «(для … класса I)»
    text, dropped = A._drop_other_cases("Какое сопротивление изоляции у электроинструмента класса II?",
                                        "Сопротивление изоляции должно быть не менее 0,5 МОм [1].",
                                        [make_hit(P52, "ot_instrument_835n", "52")])
    assert dropped == 0 and "0,5" in text
    assert A.NO_DATA in A._strip_clause_refs("Сопротивление не нормируется [1]. Нет_данных")


def test_letters_pair_and_past_purchases():
    t = tkp.parse("Просим предложить пять мультиметров для тока до 10 А и пару паяльных станций до 450 °C", {"all"})
    assert [(p.kind, p.qty) for p in t.positions] == [("Мультиметр", 5), ("Паяльная станция", 2)]
    t = tkp.parse("Здравствуйте! В прошлый раз брали у вас 3 паяльные станции. Сейчас просим КП на один "
                  "осциллограф 100 МГц.", {"all"})
    assert [(p.kind, p.qty) for p in t.positions] == [("Осциллограф", 1)]
    t = tkp.parse("Нужны такие же паяльные станции, как брали в прошлый раз, 3 шт.", {"all"})
    assert [(p.kind, p.qty) for p in t.positions] == [("Паяльная станция", 3)]     # просьба — позиция остаётся


def test_budget_is_read_with_money_unit_only():
    from kbagent import spravochnik as S
    assert S.budget("бюджет до 50 000 рублей") == 50000 and S.budget("не дороже 2 тыс. руб.") == 2000
    assert S.budget("до 50 000 руб") == 50000
    assert S.budget("полоса до 50 МГц, 2 шт.") is None and S.budget("ток до 10 А") is None
    values, _ = S.load()
    q = "Осциллограф на 2 канала с полосой не менее 70 МГц, бюджет до 50 000 рублей"
    text = S.answer(S.understand(q, values), {"all"}, q).text
    assert "В бюджет до 50\xa0000\xa0₽" in text and "АКИП-4115/1В** — 38\xa0900\xa0₽" in text and "Дороже бюджета" in text
    t = tkp.parse("Просим КП: 2 осциллографа с полосой не менее 150 МГц и 4 каналами. Бюджет до 300 000 рублей.", {"all"})
    assert t.budget == 300000 and "Бюджет клиента — до 300\xa0000\xa0₽: итог" in tkp.answer_text(t)


def test_term_from_another_found_fragment_repairs_reference_invented_term_refuses(monkeypatch):
    """«DCV» есть на соседней странице руководства — ответ остаётся, ссылка дополняется; «Mode», которого нет ни в
    одном найденном фрагменте, — выдумка: отказ."""
    from kbagent import llm
    from kbagent.retriever import Retrieval
    hits = [make_hit("Максимальное напряжение: не более 1000 В.", "jupiter", "1"),
            make_hit("Постоянное напряжение DCV: пределы 200 мВ … 1000 В.", "jupiter", "2")]
    retrieval = Retrieval(hits=hits, best_score=0.99, confident=True, scored_by="reranker")

    def model(answer):
        monkeypatch.setattr(llm, "chat", lambda messages, max_tokens=None, temperature=0.0: (answer, {"seconds": 0.1}))

    model("Максимальное входное напряжение — 1000 В для DCV [1].")
    a = A.answer_from_documents("Какое максимальное входное напряжение у Юпитер 830B?", retrieval)
    assert a.status == "answer" and a.stats["refs_repaired"] == [2] and "1000 В" in a.text
    assert "2]" in a.text                                                   # ссылка на фрагмент с «DCV» добавлена
    model("Нажмите DISPLAY и выберите XY Mode [1].")
    a = A.answer_from_documents("Как включить режим XY?", retrieval)
    assert a.status == "model_refused" and a.stats["unsupported_terms"] == ["DISPLAY", "Mode"]

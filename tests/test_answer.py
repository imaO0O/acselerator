"""Проверки ответа: ссылки, нумерация, служебные строки, отказы, запреты, перенос между случаями."""
from conftest import make_hit

from kbagent import answer as A


def test_ref_numbers_lists_and_ranges():
    assert A._ref_numbers("1, 3") == {1, 3}
    assert A._ref_numbers("1–3") == {1, 2, 3}
    assert A._valid_refs("ответ [1, 9] и [2–3]", 4) == [1, 2, 3]


def test_clean_refs_drops_unknown_numbers():
    assert A._clean_refs("факт [1, 9]. ещё [9].", [1]) == "факт [1]. ещё ."


def test_renumber_starts_from_one_in_order_of_appearance():
    hits = [make_hit(f"фрагмент {k}", clause=str(k)) for k in range(1, 5)]
    text, new_hits, cited = A._renumber("Первое [4]. Второе [2].", hits, [2, 4])
    assert text == "Первое [1]. Второе [2]."
    assert [h.chunk.clause for h in new_hits[:2]] == ["4", "2"] and cited == [1, 2]


def test_merge_and_service_lines():
    assert A._merge_refs("хранится отдельно [4] [1][3][2][4].") == "хранится отдельно [1, 2, 3, 4]."
    assert A._strip_service_lines("Действие [1].\nОснование: [1].") == "Действие [1]."


def test_refusal_disguised_as_answer():
    assert A._is_refusal("Нет. В предоставленных фрагментах нет информации об этом [1, 2]")
    assert not A._is_refusal("Нет. Наряд-допуск продлевается только 1 раз [1].")


def test_open_question_detection():
    assert A.OPEN_QUESTION_RE.match("Что делать при искрении?")
    assert A.OPEN_QUESTION_RE.match("На какой срок выдаётся наряд?")
    assert not A.OPEN_QUESTION_RE.match("Почему наряд можно продлевать сколько угодно раз?")


def test_prohibitions_from_cited_clause_are_added():
    hit = make_hit("3.1 Нажать кнопку аварийной остановки. 3.3 Работнику запрещается самостоятельно "
                   "вскрывать оборудование и устранять неисправность.")
    text = A._add_prohibitions("Что делать при искрении оборудования?", "1. Нажать кнопку остановки [1].", [hit], [1])
    assert "запрещается самостоятельно вскрывать оборудование" in text


def test_prohibitions_unrelated_to_question_are_not_added():
    hit = make_hit("4.3 Браслет, не прошедший проверку, к работе не допускается.")
    text = A._add_prohibitions("Можно ли выдать конденсаторы в производство?", "Нет [1].", [hit], [1])
    assert "Браслет" not in text


def test_claim_for_other_category_is_dropped():
    hits = [make_hit("3.1 При отказе категории А работник немедленно сообщает диспетчеру по телефону 2211."),
            make_hit("4.1 Мастер участка в течение 30 минут регистрирует отказ в журнале.", clause="4.1")]
    text, dropped = A._drop_other_cases("Что делать при отказе категории Б?",
                                        "1. Сообщить диспетчеру по телефону 2211 [1].\n"
                                        "2. Мастер в течение 30 минут регистрирует отказ [2].", hits)
    assert dropped == 1 and "2211" not in text and text.startswith("1. Мастер")


def test_claim_kept_when_category_matches():
    hits = [make_hit("3.1 При отказе категории А работник немедленно сообщает диспетчеру по телефону 2211.")]
    text, dropped = A._drop_other_cases("Что делать при отказе категории А?", "Сообщить по 2211 [1].", hits)
    assert dropped == 0

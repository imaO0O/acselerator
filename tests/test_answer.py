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
    assert A._merge_refs("после снятия карантина [1, 2], [2].") == "после снятия карантина [1, 2]."
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


def test_leading_reference_moves_to_sentence_end():
    text = "[1] Категория А — отказ (искрение). [2] При отказе нажать кнопку, сообщить по п. 3.1 на 2211."
    assert A._refs_to_end(text) == ("Категория А — отказ (искрение) [1]. "
                                    "При отказе нажать кнопку, сообщить по п. 3.1 на 2211 [2].")
    assert A._refs_to_end("1. [1] Нажать кнопку.") == "1. Нажать кнопку [1]."
    assert A._refs_to_end("Срок 15 дней [1].") == "Срок 15 дней [1]."


def test_garbled_word_is_restored_from_fragment():
    hits = [make_hit("Продлевать наряд-допуск имеет право работник, выдавший наряд-допуск.")]
    text, fixed = A._fix_garbled("Продление наряда-одпуска — работнику, выдавшему наряд-одпуск. Допускается 1 раз.",
                                 hits, "Кто продлевает наряд-допуск?")
    assert text == "Продление наряда-допуска — работнику, выдавшему наряд-допуск. Допускается 1 раз."
    assert len(fixed) == 2           # формы слов («работнику», «Допускается») не трогаются


def test_neighbor_fragment_inherits_score_of_found_clause():
    found, weak = make_hit("пункт 3.1", clause="3.1", score=0.9), make_hit("слабый", doc_id="other", score=0.03)
    found.chunk.id, weak.chunk.id = "doc#4", "other#9"
    neighbor = make_hit("продолжение пункта 3.1", clause="3.2", score=0.0)
    neighbor.chunk.id, neighbor.neighbor = "doc#5", True
    hits = [found, weak, neighbor]
    assert A._support(neighbor, hits) == 0.9 and A._support(weak, hits) == 0.03


def test_latin_letter_inside_russian_word_is_fixed():
    hits = [make_hit("Срок действия наряда-допуска — не более 15 календарных дней.")]
    text, fixed = A._fix_garbled("Срок действия наряда-доcпуска — 15 дней.", hits, "Срок наряда?")
    assert "наряда-допуска" in text and fixed


def test_ban_without_support_in_fragment_is_dropped():
    hits = [make_hit("Защита от перегрева. Если температура превышает лимит, прибор отключается. "
                     "Не используйте станцию вблизи горючих газов.")]
    text, dropped = A._drop_unsupported_bans(
        "Прибор отключается при перегреве [1]. Запуск станции в обход защиты не допускается [1].", hits)
    assert dropped == 1 and text == "Прибор отключается при перегреве [1]."
    kept, dropped = A._drop_unsupported_bans("Не используйте станцию вблизи горючих газов [1].", hits)
    assert dropped == 0

"""Справочник параметров: проверка данных при загрузке, понимание вопроса, вывод «подходит / не подходит» программой."""
from kbagent import spravochnik as S

ALL = {"all", "production", "metrology", "admin"}
HEAD = "Модель;Вид;Параметр;Значение;Единица;Условие;Документ;Страница;Цитата\n"


def test_import_checks_quality_and_does_not_duplicate(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text(HEAD +
                 "X-100;Мультиметр;Постоянный ток, верхний предел;10;А;;doc1;2;10A\n"
                 "X-100;Мультиметр;Постоянный ток, верхний предел;10;А;;doc1;2;10A\n"        # повтор — не дублируется
                 "X-100;Мультиметр;Масса;;г;;doc1;2;\n"                                    # пусто
                 "X-100;Мультиметр;Масса;160;фунт;;doc1;2;\n"                              # неизвестная единица
                 "X-100;Мультиметр;Масса;160;г;;doc1;две;\n"                               # страница не число
                 "X-100;Мультиметр;Масса;160;г;;nodoc;2;\n"                                # документа нет в базе
                 "X-200;Осциллограф;Полоса пропускания;100;МГц;;doc1;6;\n"
                 "X-200;Осциллограф;Полоса пропускания;0,12;ГГц;;doc1;9;\n",               # 100 МГц ≠ 120 МГц
                 encoding="utf-8")
    values, rep = S.parse(f, known_docs={"doc1"})
    assert rep.rows == 8 and len(values) == 3 and rep.duplicates == 1
    assert len(rep.errors) == 4
    assert [c["model"] for c in rep.contradictions] == ["X-200"]


def test_same_value_in_other_units_is_not_a_contradiction(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text(HEAD + "X-200;Осциллограф;Полоса пропускания;100;МГц;;doc1;6;\n"
                        "X-200;Осциллограф;Полоса пропускания;0,1;ГГц;;doc1;9;\n", encoding="utf-8")
    _, rep = S.parse(f, known_docs={"doc1"})
    assert rep.contradictions == []


def test_questions_are_understood_and_checked_by_program():
    values, rep = S.load()
    assert rep.errors == [] and rep.values >= 100
    q = S.understand("Можно ли мультиметром MAS830L измерить ток 15 А?", values)
    assert q.models == ["MAS830L"] and q.reqs[0].mode == "min" and q.reqs[0].value == 15
    a = S.answer(q, ALL, "")
    assert "не подходит" in a.text and "10 А" in a.text and a.sources[0]["doc_id"] == "multimeter_mas830l"
    # подбор по нескольким параметрам: «не менее» читается раньше «менее»
    q = S.understand("Нужен осциллограф с полосой не менее 150 МГц и 4 каналами", values)
    assert [r.mode for r in q.reqs] == ["min", "min"]
    assert "Подходят: 6 из" in S.answer(q, ALL, "").text
    # обозначение модели не принимается за требование («4115/1В» — не «1 В»)
    q = S.understand("Какая полоса пропускания у АКИП-4115/2В?", values)
    assert q.models == ["АКИП-4115/2В"] and q.reqs == []
    # температура — внутри диапазона
    q = S.understand("Подойдёт ли SBK936B для пайки при 450 °C?", values)
    assert q.reqs[0].mode == "inside" and "подходит" in S.answer(q, ALL, "").text


def test_contradiction_is_shown_and_sent_to_expert(tmp_settings):
    from kbagent.experts import load
    values, _ = S.load()
    q = S.understand("Какая частота дискретизации у АКИП-4115/1В?", values)
    a = S.answer(q, ALL, "Какая частота дискретизации у АКИП-4115/1В?")
    assert "Источники расходятся" in a.text and "1 Гвыб/с" in a.text and "1 Гц/с" in a.text
    assert load()[0]["subject"].startswith("АКИП-4115/1В")
    S.answer(q, ALL, "ещё раз")                                    # открытый вопрос повторно не заводится
    assert len(load()) == 1 and load()[0]["asked"] == 2


def test_document_questions_are_not_taken_by_reference():
    values, _ = S.load()
    for question in ["Как заменить батарею в MAS830L?", "Какой плавкий предохранитель установлен в мультиметре MAS830L?",
                     "При какой температуре окружающего воздуха допускается эксплуатация паяльной станции «Магистр Ц20-ДВ»?",
                     "Как часто нужно включать осциллограф АКИП-4115 при длительном хранении?",
                     "Сколько стоит мультиметр TDM?", "Какое сопротивление должно быть у антистатического браслета?"]:
        assert S.understand(question, values) is None, question

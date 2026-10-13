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


def test_client_letter_becomes_tkp_positions_without_model(tmp_path, monkeypatch):
    from kbagent import experts, tkp
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    letter = ("Добрый день! Просим направить коммерческое предложение: 2 осциллографа с полосой не менее 150 МГц "
              "и 4 каналами; мультиметр для измерения постоянного тока до 15 А — 3 шт.; паяльная станция для пайки "
              "при 450 °C; источник питания 30 В 5 А.")
    t = tkp.parse(letter, {"all"})
    assert tkp.is_request(letter, t)
    osc, mm, st, ps = t.positions
    assert (osc.kind, osc.qty, osc.status, osc.model, osc.fit_count) == ("Осциллограф", 2, "offer", "АКИП-4143/1", 6)
    assert (mm.qty, mm.model, mm.status) == (3, "DT9205A", "offer") and all(c.ok for c in mm.checks)
    assert st.status == "offer" and st.reqs == ["температура жала паяльника 450 °C"]
    assert ps.status == "not_in_catalog" and ps.kind == "Источник питания"
    text = tkp.answer_text(t)
    assert "Предлагаем **АКИП-4143/1**" in text and "нет в справочнике" in text
    assert not experts.load()                      # разбор ничего не записывает сам


def test_tkp_is_not_triggered_by_ordinary_questions(tmp_path, monkeypatch):
    from kbagent import experts, tkp
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    for q in ["Клиенту нужен осциллограф с полосой не менее 150 МГц и 4 каналами — что предложить?",
              "Сколько осциллографов и мультиметров на поверке?", "Где лежат паяльные станции и мультиметры?",
              "Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)?", "Что делать при искрении оборудования?"]:
        assert not tkp.is_request(q, tkp.parse(q, {"all"})), q
    t = tkp.parse("Подготовь ТКП: нужен мультиметр", {"all"})
    assert tkp.is_request("Подготовь ТКП: нужен мультиметр", t)
    assert t.positions[0].status == "no_reqs" and "уточните" in t.positions[0].ask_client


def test_tkp_word_document_has_positions_table(tmp_path, monkeypatch):
    import io

    from docx import Document

    from kbagent import experts, tkp
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    t = tkp.parse("ТКП: 2 осциллографа от 150 МГц на 4 канала, мультиметр на ток до 15 А", {"all"}).to_dict()
    text = tkp.draft_text(t, "Сотрудник")
    assert "АКИП-4143/1 — 2 шт." in text and "[заполняет менеджер]" in text
    doc = Document(io.BytesIO(tkp.tkp_docx(text, t, "Сотрудник", "13.10.2026 12:00", "Агент")))
    cells = [c.text for c in doc.tables[0].rows[1].cells]
    assert cells[1] == "Осциллограф АКИП-4143/1" and cells[2] == "2" and "соответствует" in cells[3]


def test_expert_decision_is_used_in_later_answers(tmp_path, monkeypatch):
    from kbagent import experts
    from kbagent import spravochnik as spr
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    q = spr.understand("Какая частота дискретизации у АКИП-4115/1В?")
    first = spr.answer(q, {"all"}, "вопрос")
    assert "Источники расходятся" in first.text and len(experts.load()) == 1
    it = experts.load()[0]
    right = next(d for d in it["details"] if d["value"].startswith("1 Гвыб"))
    experts.resolve(it["id"], "Эксперт", "на с. 6 опечатка", right)
    again = spr.answer(q, {"all"}, "вопрос")
    assert "подтверждено экспертом" in again.text and "Источники расходятся" not in again.text
    assert len(experts.load()) == 1                # вопрос эксперту повторно не заводится
    pick = spr.answer(spr.understand("Осциллограф с частотой дискретизации от 800 Мвыб/с"), {"all"}, "вопрос")
    assert "АКИП-4115/1В" in pick.text.split("Не подходят")[0]


def test_model_comparison_lists_differences():
    from kbagent import spravochnik as spr
    a = spr.answer(spr.understand("Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)?"), {"all"}, "вопрос")
    assert "Различаются: частота дискретизации на канал" in a.text


def test_letter_text_is_read_from_word_text_and_email():
    import io

    from docx import Document

    from kbagent import tkp
    doc = Document()
    doc.add_paragraph("Просим КП: 2 осциллографа от 150 МГц на 4 канала")
    buf = io.BytesIO()
    doc.save(buf)
    assert "2 осциллографа" in tkp.letter_text("Письмо.DOCX", buf.getvalue())
    assert "мультиметр" in tkp.letter_text("p.txt", "Нужен мультиметр до 15 А".encode("cp1251"))
    eml = ("Subject: =?utf-8?b?" + __import__("base64").b64encode("Запрос КП".encode()).decode() + "?=\n"
           "Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
           "Нужна паяльная станция до 450 °C").encode("utf-8")
    text = tkp.letter_text("m.eml", eml)
    assert text.startswith("Запрос КП") and "паяльная станция" in text
    t = tkp.parse(text, {"all"})
    assert tkp.is_request(text, t) and t.positions[0].kind == "Паяльная станция"


def test_letter_only_with_unknown_products_is_an_honest_refusal(tmp_path, monkeypatch):
    from kbagent import experts, tkp
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    text = "Подготовьте ТКП на лабораторный источник питания 30 В 5 А и генератор сигналов"
    t = tkp.parse(text, {"all"})
    assert tkp.is_request(text, t) and not tkp.has_offer(t)
    assert "нет в справочнике" in tkp.answer_text(t)

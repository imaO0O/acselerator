"""Черновик справочника из руководства: способы чтения таблиц, добавление отмеченного, качество на демо."""
import json
from collections import defaultdict

import pytest

from kbagent import spravochnik as S
from kbagent import spravochnik_draft as D


def chunk(text, page=5):
    return {"text": text, "page_start": page}


def by(rows):
    return {(r["Модель"], r["Параметр"]): (r["Значение"], r["Единица"], r["Условие"]) for r in rows}


def test_tables_are_read_by_columns_rows_and_limits():
    osc = [chunk("3.1.1 Число каналов вертикального отклонения – 2; Полоса пропускания 100МГц 200МГц "
                 "Время нарастания ≤3,5 нс ≤2,2 нс", 6),
           chunk("АКИП-4122/2(V) 100 МГц 500 МГц / 1 ГГц", 7)]
    rows = by(D.candidates(osc, ["АКИП-4115/1В", "АКИП-4115/2В"], "Осциллограф"))
    assert rows[("АКИП-4115/1В", "Полоса пропускания")] == ("100", "МГц", "")          # столбцы по моделям
    assert rows[("АКИП-4115/2В", "Полоса пропускания")] == ("200", "МГц", "")
    assert rows[("АКИП-4115/2В", "Время нарастания")] == ("2,2", "нс", "не более")
    assert rows[("АКИП-4115/1В", "Число каналов")] == ("2", "", "")
    row = by(D.candidates(osc, ["АКИП-4122/2(V)"], "Осциллограф"))                     # строка таблицы с моделью
    assert row[("АКИП-4122/2(V)", "Полоса пропускания")] == ("100", "МГц", "")
    assert row[("АКИП-4122/2(V)", "Частота дискретизации на канал")] == ("500", "Мвыб/с", "")
    dmm = [chunk("Постоянное напряжение DCV: 200 мВ / 2 В / 20 В / 200 В / 1000 В. Вес около 160гр.", 2)]
    rows = by(D.candidates(dmm, ["X-1"], "Мультиметр"))
    assert rows[("X-1", "Постоянное напряжение, верхний предел")] == ("1000", "В", "")   # предел — наибольшее
    assert rows[("X-1", "Масса")] == ("160", "г", "")
    solder = by(D.candidates([chunk("Температурный диапазон 80 – 500 °C. Потребляемая мощность 90 Вт")], ["S90"],
                             "Паяльная станция"))
    assert solder[("S90", "Температура жала паяльника")] == ("80–500", "°C", "")
    assert solder[("S90", "Потребляемая мощность")] == ("90", "Вт", "")


def test_title_gives_models_and_kind():
    assert D.models_from_title("Осциллографы цифровые АКИП-4143/1, 4143/1А, 4143/2") == \
        ["АКИП-4143/1", "АКИП-4143/1А", "АКИП-4143/2"]
    assert D.kind_of("Паяльная станция цифровая CXG DES H95") == "Паяльная станция"


def test_marked_rows_go_through_spravochnik_check(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DIR", tmp_path)
    monkeypatch.setattr(S, "PATH", tmp_path / "pribory.csv")
    monkeypatch.setattr("kbagent.ingest.load_catalog", lambda raw: {"doc1": {}})
    S._CACHE.clear()
    (tmp_path / "pribory.csv").write_text(";".join(S.COLUMNS) + "\nX-1;Мультиметр;Масса;160;г;;doc1;2;Вес 160 г\n",
                                          encoding="utf-8-sig")
    rows = [{"Модель": "X-1", "Вид": "Мультиметр", "Параметр": "Постоянное напряжение, верхний предел",
             "Значение": "1000", "Единица": "В", "Условие": "", "Страница": 2, "Цитата": "… 1000 В"},
            {"Модель": "X-1", "Вид": "Мультиметр", "Параметр": "Масса", "Значение": "160", "Единица": "г",
             "Условие": "", "Страница": 2, "Цитата": "Вес 160 г"}]                    # повтор — не дублируется
    rep = D.add_rows(rows, "doc1")
    assert rep.values == 2 and len(S.PATH.read_text(encoding="utf-8-sig").splitlines()) == 3   # повтор отсеян
    S._CACHE.clear()


def test_draft_finds_most_of_manual_spravochnik_on_demo():
    from kbagent.config import settings
    path = settings.index_dir / "chunks.jsonl"
    if not path.exists():
        pytest.skip("индекса нет (он не хранится в git) — проверка на компьютере с базой знаний")
    chunks = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        chunks[c["doc_id"]].append(c)
    values, _ = S.load()
    r = D.check(chunks, values)
    assert r["total"] >= 100 and r["found"] / r["total"] >= 0.8            # 31.10: 86 из 104

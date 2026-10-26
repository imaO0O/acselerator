"""Поиск по обозначениям: модели одной серии, децимальные номера, ТУ — совпадение целиком, а не по кускам."""
import numpy as np

from kbagent.ingest import Chunk
from kbagent.store import Index
from kbagent.text import designation_tokens

TEXTS = {
    "a1": "Осциллограф АКИП-4122/1: полоса пропускания 20 МГц, 2 канала.",
    "a10": "Осциллограф АКИП-4122/10: полоса пропускания 200 МГц, 4 канала.",
    "b1": "Анализатор АКИП 4115/1В: частота дискретизации 1 Гвыб/с.",
    "b2": "Анализатор АКИП-4115/2В: частота дискретизации 2 Гвыб/с.",
    "dn1": "Корпус блока питания АБВГ.468157.001 — сборочный чертёж, лист 1.",
    "dn2": "Корпус блока питания АБВГ.468157.002 — сборочный чертёж, лист 1.",
    "tu": "Изготовлено по ТУ 4221-001-12345678-2015, поставляется с паспортом.",
    "tu2": "Изготовлено по ТУ 4221-002-12345678-2015, поставляется с паспортом.",
}


def make_index() -> Index:
    chunks = [Chunk(id=k, doc_id=k, doc_title="Документ", doc_kind="Руководство", access="all", clause=None,
                    heading=None, page_start=1, page_end=1, text=t) for k, t in TEXTS.items()]
    return Index(chunks, np.zeros((len(chunks), 4), dtype=np.float32))


def top(index: Index, query: str) -> str:
    return index.chunks[index.search_words(query, {"all"}, 3)[0][0]].id


def test_designation_is_one_token_whatever_the_spelling():
    same = ["АКИП-4115/2В", "акип 4115/2в", "АКИП 4115/2В", "AKИП-4115/2B"]           # латинские A, K, B
    assert all("code:41152b" in designation_tokens(s) for s in same)
    assert "code:41151b" not in designation_tokens("АКИП-4115/2В")
    assert designation_tokens("до 15 А при 450 °C, 150МГц, ДО 20 А") == []          # величины — не обозначения
    assert "dn:468157001" in designation_tokens("по чертежу 468157.001")
    assert "tu:4221001123456782015" in designation_tokens("ТУ 4221-001-12345678-2015")


def test_model_of_a_series_is_found_exactly():
    index = make_index()
    assert top(index, "Какая полоса пропускания у АКИП-4122/10?") == "a10"
    assert top(index, "полоса акип 4122/1") == "a1"
    assert top(index, "дискретизация АКИП 4115/2В") == "b2"
    assert top(index, "дискретизация AKИП-4115/1B") == "b1"                          # латиница вместо кириллицы


def test_decimal_number_and_tu_are_found_exactly():
    index = make_index()
    assert top(index, "чертёж АБВГ.468157.002") == "dn2"
    assert top(index, "чертёж 468157.001") == "dn1"
    assert top(index, "паспорт по ТУ 4221-002-12345678-2015") == "tu2"

"""Проверочные наборы новых функций — то, что можно проверить без модели, в каждом прогоне тестов.

eval/voice.yaml — фраза, как её выдаёт распознавание речи, приводится к языку справочника, и справочник отвечает
программой то же, что на набранный вопрос. eval/asof.yaml — в поиск попадает редакция, действовавшая на дату вопроса,
а действующая сейчас, если она другая, — нет. Ответы модели по этим наборам — eval/run_eval.py на компьютере с моделями.
"""
from pathlib import Path

import yaml

from kbagent import asof, spravochnik, voice
from kbagent.config import settings
from kbagent.ingest import registry

ROOT = Path(__file__).resolve().parents[1]


def _set(name: str) -> list[dict]:
    return yaml.safe_load((ROOT / "eval" / name).read_text(encoding="utf-8"))["questions"]


def test_voice_set_reaches_the_same_reference_answers():
    from eval.run_eval import has_facts
    models = {v.model for v in spravochnik.load()[0]}
    allowed = settings.roles["Техпродажи и сервис"]
    checked = 0
    for q in _set("voice.yaml"):
        assert voice.normalize(q["spoken"], models) == q["question"], q["id"]
        query = spravochnik.understand(q["question"])
        if query is None:
            continue                      # документный вопрос — отвечает модель
        a = spravochnik.answer(query, allowed, question=q["question"])
        assert a.status == "answer" and has_facts(a.text, q["must_include"]), (q["id"], a.text[:300])
        checked += 1
    assert checked == 6


def test_asof_set_searches_the_edition_in_force_then():
    catalog = registry(ROOT / "data" / "raw")
    docs = set(catalog)
    for q in _set("asof.yaml"):
        window = asof.parse(q["question"])
        if q["group"].startswith("без даты"):
            assert window is None, q["id"]
            continue
        assert window is not None, q["id"]
        scope = asof.scope(window, settings.roles["Инженер производства"], docs, catalog)
        if q["type"] == "none":
            assert "STP-DP-01_vkhodnoj_kontrol" not in scope, q["id"]
            continue
        gold = q["source"]["doc"]
        assert gold in scope, q["id"]
        now = asof.current_edition(gold, catalog)
        assert now == gold or now not in scope, q["id"]     # действующая сейчас редакция — не в поиске

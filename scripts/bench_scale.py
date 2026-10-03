"""Замер поиска на большом корпусе: индекс размножается до N фрагментов (по умолчанию 100 000).

Проверяет, что точный перебор векторов в numpy и BM25 в памяти укладываются в доли секунды
на корпусе крупного предприятия. Векторы копий слегка зашумляются, чтобы не совпадать.
Переранжировщик и языковая модель от размера корпуса не зависят (получают 20 кандидатов
и 5 фрагментов), поэтому здесь не замеряются.

    python scripts/bench_scale.py            # 100 000 фрагментов
    python scripts/bench_scale.py 300000
"""
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import llm  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.store import Index  # noqa: E402

QUESTIONS = [
    "Какой срок входного контроля для электрорадиоизделий?",
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск?",
    "Какое сопротивление должно быть у антистатического браслета?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "Как часто включать осциллограф при длительном хранении?",
    "По какой форме ЭРИ выдаются в производство?",
    "Какой температурный диапазон у паяльного фена?",
]


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    base = Index.load(settings.index_dir)
    copies = -(-target // len(base.chunks))
    rng = np.random.default_rng(0)

    t0 = time.time()
    chunks = [replace(c, id=f"{c.id}#{k}") for k in range(copies) for c in base.chunks][:target]
    vectors = np.vstack([base.vectors + (rng.normal(0, 0.01, base.vectors.shape).astype(np.float32) if k else 0)
                         for k in range(copies)])[:target]
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    big = Index(chunks, vectors)
    build = time.time() - t0

    allowed = set().union(*settings.roles.values())
    qvecs = llm.embed(QUESTIONS)
    words_t, meaning_t = [], []
    for q, v in zip(QUESTIONS, qvecs):
        t = time.perf_counter()
        big.search_words(q, allowed, settings.candidates)
        words_t.append(time.perf_counter() - t)
        t = time.perf_counter()
        big.search_meaning(v, allowed, settings.candidates)
        meaning_t.append(time.perf_counter() - t)

    ms = lambda xs: f"{statistics.median(xs) * 1000:.0f} мс"  # noqa: E731
    lines = [
        f"# Поиск на {len(chunks):,} фрагментах".replace(",", " "), "",
        f"Исходный индекс: {len(base.chunks)} фрагментов, размножен в {copies} раз; векторы bge-m3, 1024 числа.", "",
        "| Этап | Время |", "|---|---|",
        f"| Построение индекса по словам (BM25) в памяти | {build:.0f} с |",
        f"| Поиск по словам, медиана на вопрос | {ms(words_t)} |",
        f"| Поиск по смыслу (точный перебор numpy), медиана | {ms(meaning_t)} |",
        f"| Память под векторы | {vectors.nbytes / 2**20:.0f} МБ |", "",
        "Переранжировщик и языковая модель получают одинаковое число кандидатов при любом размере корпуса,",
        "поэтому общее время ответа растёт только на время поиска из таблицы.",
    ]
    out = ROOT / "eval" / "results" / "scale.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

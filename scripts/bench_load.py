"""Нагрузочный замер: N сотрудников задают вопросы одновременно.

    python scripts/bench_load.py            # 1, 4 и 8 одновременных пользователей
    python scripts/bench_load.py 16

Результат: eval/results/load.md. Серверы моделей обрабатывают запросы параллельно
(слоты llama-server, ключ -np в scripts/start_servers.ps1).
"""
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent.agent import Agent  # noqa: E402

QUESTIONS = [
    "Какой срок входного контроля для ЭРИ?",
    "Что делать при искрении оборудования?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "На какой срок выдаётся наряд-допуск?",
    "Какое сопротивление должно быть у антистатического браслета?",
    "Какое оборудование сейчас находится в ремонте?",
    "Сколько часов можно держать микросхемы MSL 3 после вскрытия упаковки?",
    "Каким ярлыком маркируют продукцию, прошедшую входной контроль?",
]


def main():
    levels = [int(a) for a in sys.argv[1:]] or [1, 4, 8]
    base = Agent()
    lines = ["# Одновременные пользователи", "", "| Пользователей | Медиана ответа, с | Максимум, с | "
             "Ответов в минуту |", "|---|---|---|---|"]
    for n in levels:
        qs = [QUESTIONS[i % len(QUESTIONS)] for i in range(n)]
        t0 = time.time()
        with ThreadPoolExecutor(n) as ex:
            times = list(ex.map(lambda q: Agent(base.index).ask(q).seconds, qs))
        wall = time.time() - t0
        lines.append(f"| {n} | {statistics.median(times):.1f} | {max(times):.1f} | {n / wall * 60:.0f} |")
        print(lines[-1])
    (ROOT / "eval" / "results" / "load.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

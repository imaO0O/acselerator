"""Где тратится время ответа: по этапам и отдельно — обработка подсказки и генерация текста.

    python scripts/profile_answer.py

Нужен, чтобы настраивать работу на ноутбуке без видеокарты: на процессоре дольше всего
обрабатывается длинная подсказка с фрагментами, а не печатается ответ.
"""
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import answer as answer_mod  # noqa: E402
from kbagent import llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402

QUESTIONS = [
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск на работы в электроустановках?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "Сколько часов можно держать микросхемы MSL 3 после вскрытия упаковки?",
    "Какое сопротивление должно быть у антистатического браслета?",
    "Какое оборудование сейчас находится в ремонте?",
]


def main():
    calls = []
    original = llm._post

    def traced(url, payload):            # запоминаем время каждого обращения к серверам моделей
        t0 = time.time()
        data = original(url, payload)
        kind = url.rsplit("/", 1)[-1]
        t = data.get("timings", {}) if isinstance(data, dict) else {}
        calls.append({"kind": kind, "seconds": time.time() - t0, "prompt_n": t.get("prompt_n"),
                      "prompt_ms": t.get("prompt_ms"), "gen_n": t.get("predicted_n"), "gen_ms": t.get("predicted_ms")})
        return data
    llm._post = traced

    agent = Agent()
    totals = []
    for q in QUESTIONS:
        calls.clear()
        t0 = time.time()
        r = agent.ask(q)
        total = time.time() - t0
        totals.append(total)
        print(f"\n{q}  →  {r.route}/{r.status}, всего {total:.1f} с")
        for c in calls:
            extra = (f"подсказка {c['prompt_n']} ток. за {c['prompt_ms'] / 1000:.1f} с, "
                     f"текст {c['gen_n']} ток. за {c['gen_ms'] / 1000:.1f} с") if c["prompt_ms"] else ""
            print(f"  {c['kind']:<16} {c['seconds']:5.2f} с  {extra}")
    print(f"\nМедиана: {statistics.median(totals):.1f} с")
    del answer_mod


if __name__ == "__main__":
    main()

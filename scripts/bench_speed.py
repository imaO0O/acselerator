"""Замер скорости на текущем компьютере.

Запустите серверы моделей (scripts\\start_servers.bat, на ноутбуке без
NVIDIA — с ключом -Cpu) и выполните:

    python scripts/bench_speed.py
    python scripts/bench_speed.py cpu      # подпись к замеру (например, режим без видеокарты)

Результат печатается и сохраняется в eval/results/speed_<имя компьютера>.md,
чтобы собрать замеры со всех ноутбуков команды в одну таблицу.
"""
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402

QUESTIONS = [
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск на работы в электроустановках?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "Сколько часов можно держать микросхемы MSL 3 после вскрытия упаковки?",
    "Сколько стоит поверка одного осциллографа?",
]


def machine() -> dict:
    info = {"Компьютер": socket.gethostname(), "ОС": f"{platform.system()} {platform.release()}",
            "Процессор": platform.processor() or platform.machine(), "Потоков CPU": os.cpu_count()}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        info["Видеокарта"] = out or "нет"
    except (OSError, subprocess.TimeoutExpired):
        info["Видеокарта"] = "нет (NVIDIA не найдена)"
    try:
        import ctypes

        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                        ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
        m = MEM()
        m.dwLength = ctypes.sizeof(MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        info["ОЗУ"] = f"{m.ullTotalPhys / 2**30:.0f} ГБ"
    except (AttributeError, OSError):
        pass
    return info


def main():
    status = llm.health()
    if not all(status.values()):
        sys.exit(f"Не все серверы моделей запущены: {status}. Запустите scripts\\start_servers.bat")

    info = machine()
    print("\n".join(f"{k}: {v}" for k, v in info.items()))

    # генерация: прогрев и три замера
    msgs = [{"role": "user", "content": "Перечисли пять правил безопасной работы с паяльной станцией."}]
    llm.chat(msgs, max_tokens=20)
    gen = [llm.chat(msgs, max_tokens=200)[1] for _ in range(3)]
    tps = statistics.median(g["tokens_per_second"] or 0 for g in gen)

    t0 = time.time()
    llm.embed(["тестовый фрагмент документа"] * 32)
    embed_s = time.time() - t0

    agent = Agent()
    agent.ask(QUESTIONS[0])  # прогрев
    times = []
    for q in QUESTIONS:
        r = agent.ask(q, route="documents")
        times.append(r.seconds)
        print(f"{r.seconds:5.2f} с  [{r.status}]  {q}")

    result = {
        "Генерация, токенов/с": f"{tps:.0f}",
        "Векторы для 32 фрагментов, с": f"{embed_s:.2f}",
        "Ответ на вопрос, медиана, с": f"{statistics.median(times):.1f}",
        "Ответ на вопрос, максимум, с": f"{max(times):.1f}",
    }
    print("\n".join(f"{k}: {v}" for k, v in result.items()))

    label = f"_{sys.argv[1]}" if len(sys.argv) > 1 else ""
    out = ROOT / "eval" / "results" / f"speed_{socket.gethostname()}{label}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Скорость: {info['Компьютер']}{label.replace('_', ', режим ')} ({time.strftime('%Y-%m-%d')})", "", "| Параметр | Значение |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in {**info, **result}.items()]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()

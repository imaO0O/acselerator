"""Создаёт демо-базу учёта с датами от сегодняшнего дня (данные — kbagent/demo_db.py).

Агент пересоздаёт базу сам при первом вопросе за день; скрипт нужен, чтобы
сделать это вручную.

    python scripts/make_demo_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent.demo_db import main  # noqa: E402

if __name__ == "__main__":
    main()

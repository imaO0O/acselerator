r"""Обновление одной командой (kbagent/update.py). Запускается в НОВОЙ установке:

    python scripts\update.py --from D:\agent_old        перенести данные из прежней установки и запустить новую

Прежняя установка не меняется: если что-то не удалось, она запускается снова, как была. После обновления —
python scripts\check_demo.py (на компьютере с моделями) и вкладка «Администрирование» → «Внедрение».
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import update  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Обновление: данные из прежней установки — в эту")
    ap.add_argument("--from", dest="old", type=Path, required=True, help="папка прежней установки")
    args = ap.parse_args()
    try:
        rep = update.migrate(args.old, user="update")
    except update.UpdateError as e:
        print(f"Обновление не выполнено: {e}")
        return 1
    print(f"Готово: {rep['summary']}\nСостояние новой установки до переноса сохранено: {rep['safety']}\n"
          "Дальше: python scripts\\check_demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

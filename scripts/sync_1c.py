"""Цены (и остатки) из 1С → прайс для ТКП (kbagent/onec.py). Только чтение, стандартный интерфейс OData.

    python scripts/sync_1c.py               один проход
    python scripts/sync_1c.py --watch 3600  обновлять каждый час (оставить работать)

Настройки — KB_1C_URL, KB_1C_USER, KB_1C_PRICE_TYPE, KB_1C_CONFIG (bp — Бухгалтерия 3.0, ut — УТ 11 / КА 2 / ERP),
KB_1C_STOCK=1 — остатки; пароль — KB_1C_PASSWORD или data/1c_password.txt.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import onec  # noqa: E402
from kbagent.actions import log_action  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="1С → прайс для ТКП")
    ap.add_argument("--watch", type=int, default=0, help="обновлять каждые N секунд")
    args = ap.parse_args()
    while True:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            rep = onec.sync()
            log_action("prajs_1c", "1С", rep["price_type"], f"цен: {rep['prices']}; без цены: {len(rep['without_price'])}")
            print(f"{stamp}: цен из 1С {rep['prices']}, моделей без цены {len(rep['without_price'])}, "
                  f"неоднозначных позиций {len(rep['ambiguous'])}, в валюте {len(rep['foreign'])}")
            for w in rep["warnings"]:
                print(f"  внимание: {w}")
            code = 0
        except onec.OneCError as e:
            print(f"{stamp}: {e}")
            code = 1
        if not args.watch:
            return code
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())

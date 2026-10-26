"""Почтовый ящик отдела → «Входящие запросы» с разбором ТКП (kbagent/mailbox.py). Только чтение.

    python scripts/check_mail.py              один проход
    python scripts/check_mail.py --watch 300  проверять каждые 5 минут (оставить работать)

Настройки — KB_MAIL_HOST, KB_MAIL_USER, KB_MAIL_FOLDER, пароль — KB_MAIL_PASSWORD или data/mail_password.txt.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import mailbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Почта отдела → входящие запросы")
    ap.add_argument("--watch", type=int, default=0, help="проверять ящик каждые N секунд")
    args = ap.parse_args()
    while True:
        rep = mailbox.check()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if rep["error"]:
            print(f"{stamp}: {rep['error']}")
        else:
            print(f"{stamp}: новых писем {rep['checked']}, из них запросов клиента {rep['requests']}")
        if not args.watch:
            return 1 if rep["error"] else 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())

"""Первый запуск у заказчика: убрать демо-учётки и создать администратора со случайным временным паролем.

    python scripts/first_run.py                 администратор — логин admin
    python scripts/first_run.py --login sidorov --name "Сидоров П. П."

Пароли демо-учёток прототипа опубликованы в scripts/make_users.py — на установке у заказчика их быть не должно.
Временный пароль показывается один раз; при первом входе администратор задаёт свой, остальных пользователей
заводит на странице «Администрирование» → «Пользователи».
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import users  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Первый запуск у заказчика")
    ap.add_argument("--login", default="admin")
    ap.add_argument("--name", default="Администратор")
    args = ap.parse_args()
    password = users.first_run(args.login.strip().lower(), args.name)
    print("Демо-учётки удалены. Администратор создан.")
    print(f"  логин:              {args.login.strip().lower()}")
    print(f"  временный пароль:   {password}   (показывается один раз — при первом входе задайте свой)")
    left = users.demo_accounts()
    if left:
        print(f"Внимание: остались демо-учётки {', '.join(left)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

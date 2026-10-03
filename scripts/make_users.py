"""Создаёт демо-учётные записи для входа в чат (data/users.yaml — только хэши паролей).

Пароли ниже — тестовые значения для демонстрации прототипа на локальном компьютере.
Перед показом вне команды или в пилоте замените их (или подключите учётные записи
предприятия) и запустите скрипт заново.

    python scripts/make_users.py
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent.auth import hash_password  # noqa: E402
from kbagent.config import settings  # noqa: E402

# логин, имя, роль (из kbagent/config.py), тестовый пароль
DEMO_USERS = [
    ("inzhener", "Инженер производства (демо)", "Инженер производства", "demo-inzhener-2026"),
    ("metrolog", "Метролог (демо)", "Метролог", "demo-metrolog-2026"),
    ("admin", "Администратор (демо)", "Администратор", "demo-admin-2026"),
]


def main():
    users = [{"login": login, "name": name, "role": role, "password_hash": hash_password(password)}
             for login, name, role, password in DEMO_USERS]
    unknown = {u["role"] for u in users} - set(settings.roles)
    if unknown:
        raise SystemExit(f"Неизвестные роли: {unknown}")
    settings.users_path.write_text(
        "# Учётные записи чата: только хэши паролей. Создаётся scripts/make_users.py\n"
        + yaml.safe_dump({"users": users}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"Учётные записи: {settings.users_path} ({len(users)})")


if __name__ == "__main__":
    main()

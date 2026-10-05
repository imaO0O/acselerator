"""Выпуск ключа доступа к API для интеграции (мессенджер, чат-бот, портал, 1С).

    python scripts/make_api_key.py "Чат-бот цеха" "Инженер производства"

Ключ показывается один раз — передайте его владельцу интеграции. В data/api_keys.yaml
сохраняется только его хэш. Отозвать ключ — удалить строку из этого файла.
"""
import getpass
import secrets
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.server import KEYS_PATH, key_hash  # noqa: E402
from kbagent.actions import log_action  # noqa: E402
from kbagent.config import settings  # noqa: E402


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in settings.roles:
        sys.exit(f"Использование: python scripts/make_api_key.py \"название\" \"роль\"\nРоли: {', '.join(settings.roles)}")
    name, role = sys.argv[1], sys.argv[2]
    key = "kb_" + secrets.token_urlsafe(32)
    data = yaml.safe_load(KEYS_PATH.read_text(encoding="utf-8")) if KEYS_PATH.exists() else {}
    keys = [k for k in (data or {}).get("keys", []) if k["name"] != name]
    keys.append({"name": name, "role": role, "hash": key_hash(key), "created": time.strftime("%Y-%m-%d %H:%M")})
    KEYS_PATH.write_text("# Ключи API: только хэши. Выпуск — scripts/make_api_key.py\n"
                         + yaml.safe_dump({"keys": keys}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    log_action("api_key", getpass.getuser(), name, f"роль: {role}")
    print(f"Ключ для «{name}» (роль: {role}) — сохраните, он показывается один раз:\n{key}")


if __name__ == "__main__":
    main()

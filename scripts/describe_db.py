"""Черновик слоя понятных названий для любой базы SQLite (или выгрузки из 1С/ERP в SQLite).

Скрипт перечисляет таблицы и поля, добавляет примеры значений и пишет YAML в формате
data/demo_db/schema.yaml. Дальше описание дописывается вместе с ответственным за систему:
что хранится в таблице, как сотрудники это называют, какие поля — номера записей (keys).
Служебные таблицы удаляются из файла — модель не будет их видеть.

    python scripts/describe_db.py путь/к/базе.sqlite > черновик.yaml
"""
import sqlite3
import sys

import yaml


def describe(path: str, samples: int = 3) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = []
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in names:
        columns = {}
        for _, col, ctype, *_ in con.execute(f'PRAGMA table_info("{table}")'):
            values = [r[0] for r in con.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT {samples}')]
            example = ", ".join(f"'{v}'" for v in values)
            columns[col] = f"TODO: что это ({ctype or 'тип не указан'}; например, {example})"
        tables.append({"name": table, "title": "TODO: что хранится в таблице", "synonyms": [],
                       "keys": [], "columns": columns})
    con.close()
    return {"tables": tables}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    print("# Черновик слоя понятных названий — заполните TODO и удалите служебные таблицы")
    print(yaml.safe_dump(describe(sys.argv[1]), allow_unicode=True, sort_keys=False))

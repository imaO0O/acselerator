"""Ответы по учётной базе: вопрос → SQL (только чтение) → ответ со ссылкой на запрос.

Модель видит только описание таблиц, а не данные. Сгенерированный запрос
проверяется (один оператор SELECT, без изменения данных) и выполняется
на соединении, открытом только для чтения. Запрос и результат показываются
пользователю — это и есть «источник» ответа.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import llm
from .config import settings
from .text import tokenize


def load_schema(path: Path) -> tuple[str, tuple[str, ...]]:
    """Описание таблиц для модели из слоя понятных названий (data/demo_db/schema.yaml)."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    blocks = []
    for t in spec["tables"]:
        head = f"{t['name']} — {t['title']}"
        if t.get("synonyms"):
            head += f" (в вопросах: {', '.join(t['synonyms'])})"
        # поля — несколькими строками (перенос, когда строка превысила 110 знаков): с одной
        # длинной строкой небольшая модель заметно чаще добавляет в запрос лишние условия
        lines, line = [], ""
        for c, d in t["columns"].items():
            line = f"{line}; {c} — {d}" if line else f"{c} — {d}"
            if len(line) > 110:
                lines.append(line + ";")
                line = ""
        if line:
            lines.append(line)
        else:
            lines[-1] = lines[-1].rstrip(";")
        blocks.append(head + "\n" + "\n".join(f"  {x}" for x in lines))
    text = "Таблицы учётной базы предприятия (SQLite):\n\n" + "\n\n".join(blocks) + "\n"
    return text, tuple(t["name"] for t in spec["tables"])


SCHEMA = yaml.safe_load(settings.db_schema.read_text(encoding="utf-8"))
SCHEMA_DESCRIPTION, TABLES = load_schema(settings.db_schema)

SQL_PROMPT = """Ты переводишь вопрос сотрудника в один SQL-запрос SQLite к учётной базе.

{schema}
Правила:
- Только один оператор SELECT. Никаких INSERT, UPDATE, DELETE, DROP, PRAGMA.
- Для поиска по наименованию используй lower(name) LIKE '%...%' по основе слова (например, '%осциллограф%').
- Текущая дата: date('now').
- Выбирай столбцы, нужные для ответа, и всегда добавляй инвентарный или номенклатурный номер, если он есть.
- Не добавляй условий, которых нет в вопросе.
- Если пишешь столбцы с псевдонимом таблицы (e.inv_no), объяви его: FROM equipment e.
- Если на вопрос нельзя ответить по этим таблицам, верни ровно: НЕТ_ДАННЫХ
Верни только SQL без пояснений.

Примеры:
Вопрос: Какие средства измерений нужно поверить в ближайший месяц?
SQL: SELECT inv_no, name, model, next_check, status FROM measuring_instruments WHERE next_check BETWEEN date('now') AND date('now', '+30 days') ORDER BY next_check
Вопрос: Сколько диодов Шоттки годно к выдаче?
SQL: SELECT part_no, name, qty, unit, batch FROM stock WHERE lower(name) LIKE '%диод%' AND lower(name) LIKE '%шоттки%' AND inspection_status = 'годен'
Вопрос: Есть ли на складе конденсаторы 100 нФ?
SQL: SELECT part_no, name, qty, unit, batch, inspection_status FROM stock WHERE lower(name) LIKE '%конденсатор%' AND lower(name) LIKE '%100 нф%'
Вопрос: Какие заявки на ремонт уже выполнены?
SQL: SELECT r.id, r.inv_no, e.name, r.created_at, r.category, r.description FROM repair_requests r JOIN equipment e ON e.inv_no = r.inv_no WHERE r.status = 'выполнена'
Вопрос: Сколько стоит обед в столовой?
SQL: НЕТ_ДАННЫХ

Вопрос: {question}
SQL:"""

ANSWER_PROMPT = """Ответь на вопрос сотрудника ТОЛЬКО по результату запроса к учётной базе.
Для каждой записи приведи номер, наименование и значения, которые отвечают на вопрос (даты, количество, статус).
Если записей несколько — оформи маркированным списком, по одной записи в строке.
Не используй технические названия столбцов (last_check, next_check) — пиши по-русски: «последняя поверка», «очередная поверка».
Числа, даты, номера и ФИО переписывай точно. Если результат пустой — так и скажи, что записей нет.
Если в вопросе предмет назван не так, как в записи (например, спрашивают про мультиметр, а в записи осциллограф), прямо укажи, как он записан в базе.
Если вопрос не о записях учёта (например, о правилах, сроках или порядке действий по регламенту) или строки результата на него не отвечают — ответь ровно одним словом: НЕТ_ДАННЫХ.
В конце ответа поставь ссылку [БД]. Отвечай кратко, по-русски.

Вопрос: {question}

Результат запроса ({count} строк):
{table}"""

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b", re.I)


def _answer_records(question: str, db_path: Path, records: list[str]) -> "DbAnswer":
    try:
        found = _lookup(db_path, records)
    except sqlite3.Error as e:
        return DbAnswer("error", f"Не удалось выполнить запрос к базе: {e}")
    if not found:
        return DbAnswer("no_data", f"В учётной базе нет записей {', '.join(records)}.")
    # текст собирается из записей напрямую, с подписями полей из слоя названий: точно и без вызова модели
    lines = []
    for _, cols, rows_, t in found:
        for row in rows_:
            pairs = "; ".join(f"{_ru(t, c)} — {v}" for c, v in zip(cols, row) if v not in (None, ""))
            lines.append(f"- {t['title'][:1].upper()}{t['title'][1:]}: {pairs}")
    text = "\n".join(lines)
    sql, columns, rows, _ = found[0]
    note = _name_mismatch(question, records, columns, rows)
    text = (f"{note}\n\n" if note else "") + text + " [БД]"
    # кратко «что это и в каком состоянии» — дополняет поиск по документам: «партия П-2609-04»
    # сама по себе не похожа на «ЭРИ, не прошедшие входной контроль», а «ЭРИ, карантин» — похожа
    summary = "; ".join(f"{_ru(t, c)} — {v}" for _, cols, rows_, t in found for row in rows_
                        for c, v in zip(cols, row) if c in SUMMARY_FIELDS and v)
    return DbAnswer("answer", text, sql="\n".join(q for q, _, _, _ in found), columns=columns,
                    rows=[list(r) for r in rows], summary=summary)


@dataclass
class DbAnswer:
    status: str                 # answer | no_data | error
    text: str
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    summary: str = ""            # для записей по номеру: наименование, вид и состояние


SUMMARY_FIELDS = {"name", "kind", "status", "inspection_status", "category", "check_type"}


def _extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    return sql


def _repair_aliases(sql: str) -> str:
    """Частая ошибка модели: «SELECT e.name FROM equipment WHERE …» — псевдоним не объявлен.
    Если не объявлен ровно один псевдоним и ровно у одной таблицы его нет — подставляем."""
    declared, bare = set(), []
    for m in re.finditer(r"\b(?:from|join)\s+(\w+)(?:\s+(?:as\s+)?(?!where|join|on|group|order|limit|left|inner)(\w+))?",
                         sql, re.I):
        table, alias = m.group(1), m.group(2)
        declared.add(table.lower())
        if alias:
            declared.add(alias.lower())
        elif table.lower() in TABLES:
            bare.append(m)
    used = {a.lower() for a in re.findall(r"\b([a-z]\w*)\.\w+", sql, re.I)}
    missing = used - declared
    if len(missing) == 1 and len(bare) == 1:
        m = bare[0]
        sql = sql[:m.end(1)] + f" {missing.pop()}" + sql[m.end(1):]
    return sql


def _fix_dates(sql: str) -> str:
    """date('now') в SQLite — время UTC, а created_at хранит дату со временем:
    «created_at <= date('now')» теряет сегодняшние записи. Берём местное время и сравниваем по дате."""
    sql = re.sub(r"\b(date|datetime)\(\s*'now'(?!\s*,\s*'localtime')", r"\1('now', 'localtime'", sql, flags=re.I)
    return re.sub(r"(?<!date\()\b((?:\w+\.)?created_at)\b(?=\s*(?:between|>=|<=|<|>|=))", r"date(\1)", sql, flags=re.I)


def _check(sql: str) -> None:
    if not re.match(r"^(select|with)\b", sql, re.I):
        raise ValueError("разрешены только запросы SELECT")
    if ";" in sql or FORBIDDEN.search(sql):
        raise ValueError("запрос изменяет данные или содержит несколько операторов")


def _like(pattern, value, escape=None):
    """LIKE без учёта регистра для кириллицы (встроенный LIKE SQLite знает только латиницу)."""
    if pattern is None or value is None:
        return None
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in str(pattern))
    return re.fullmatch(regex, str(value), re.IGNORECASE | re.DOTALL) is not None


def _run(db_path: Path, sql: str, limit: int = 50, params: tuple = ()) -> tuple[list, list]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        # встроенные lower/upper/LIKE в SQLite не знают кириллицу
        con.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        con.create_function("upper", 1, lambda s: s.upper() if isinstance(s, str) else s, deterministic=True)
        con.create_function("like", 2, _like, deterministic=True)
        con.create_function("like", 3, _like, deterministic=True)
        con.execute("PRAGMA query_only = ON")
        cur = con.execute(sql, params)
        return [d[0] for d in cur.description], cur.fetchmany(limit)
    finally:
        con.close()


def _ru(table: dict, column: str) -> str:
    """Понятное название столбца: «received» → «дата поступления»."""
    if column in table.get("labels", {}):
        return table["labels"][column]
    return re.split(r"\s*[(:]", str(table["columns"].get(column, column)))[0].strip() or column


def _lookup(db_path: Path, records: list[str]) -> list[tuple[str, list, list, dict]]:
    """Записи по номерам из вопроса — по полям-номерам (keys) слоя понятных названий, без генерации SQL."""
    found = []
    marks = ", ".join("?" * len(records))
    for t in SCHEMA["tables"]:
        for key in t.get("keys", []):
            sql = f"SELECT * FROM {t['name']} WHERE upper({key}) IN ({marks})"
            columns, rows = _run(db_path, sql, params=tuple(records))
            if rows:
                shown = sql.replace(f"({marks})", "(" + ", ".join(f"'{r}'" for r in records) + ")")
                found.append((shown, columns, rows, t))
    return found


def _name_mismatch(question: str, records: list[str], columns: list, rows: list) -> str | None:
    """«мультиметр СИ-0012», а в учёте СИ-0012 — осциллограф: говорим об этом прямо."""
    if "name" not in columns:
        return None
    generic = set(tokenize("партия заявка номер запись позиция прибор изделие оборудование инвентарный средство "
                           "измерений " + " ".join(t["title"] + " " + " ".join(t.get("synonyms", []))
                                                   for t in SCHEMA["tables"])))
    for rec in records:
        m = re.search(r"([А-Яа-яЁё]{4,})\s+" + re.escape(rec), question, re.I)
        row = next((r for r in rows if rec in {str(v).upper() for v in r}), None)
        if not m or row is None or set(tokenize(m.group(1))) <= generic:
            continue
        name = str(row[columns.index("name")])
        if not set(tokenize(m.group(1))) & set(tokenize(name)):
            return f"Обратите внимание: в учёте {rec} — «{name}» (в вопросе — «{m.group(1)}»)."
    return None


def _table(columns: list, rows: list) -> str:
    if not rows:
        return "(пусто)"
    lines = [" | ".join(columns)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    return "\n".join(lines)


def answer_from_database(question: str, db_path: Path, records: list[str] | None = None) -> DbAnswer:
    """records — номера записей из вопроса «запись + правило» («СИ-0012 просрочен — можно ли…»).
    Тогда запись ищется только по номеру, без условий из вопроса: посылка сотрудника может быть
    неверной (прибор не просрочен или это вовсе не мультиметр), и это как раз нужно показать."""
    stats = {}
    if records:
        return _answer_records(question, db_path, records)
    try:
        raw, s1 = llm.chat([{"role": "user", "content": SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION, question=question)}],
                           max_tokens=300)
    except llm.ModelError as e:
        return DbAnswer("error", f"Языковая модель недоступна: {e}")
    stats["sql_seconds"] = s1["seconds"]
    if "НЕТ_ДАННЫХ" in raw:
        return DbAnswer("no_data", "В учётной базе нет данных для ответа на этот вопрос.", stats=stats)

    sql = _fix_dates(_repair_aliases(_extract_sql(raw)))
    error = None
    for attempt in range(2):
        try:
            _check(sql)
            columns, rows = _run(db_path, sql)
            break
        except (ValueError, sqlite3.Error) as e:
            error = str(e)
            if attempt == 1:
                return DbAnswer("error", f"Не удалось выполнить запрос к базе: {error}", sql=sql, stats=stats)
            fix = SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION, question=question) + f"\n{sql}\n\nОшибка: {error}\nИсправленный SQL:"
            raw, _ = llm.chat([{"role": "user", "content": fix}], max_tokens=300)
            sql = _fix_dates(_repair_aliases(_extract_sql(raw)))

    text, s2 = llm.chat([{"role": "user", "content": ANSWER_PROMPT.format(
        question=question, count=len(rows), table=_table(columns, rows))}], max_tokens=400)
    stats.update(answer_seconds=s2["seconds"], tokens_per_second=s2.get("tokens_per_second"))
    if "НЕТ_ДАННЫХ" in text:
        return DbAnswer("no_data", "Записи учёта не отвечают на этот вопрос.", sql=sql, columns=columns,
                        rows=[list(r) for r in rows], stats=stats)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"\s*\[БД\]\s*", " ", text)).strip() + " [БД]"
    return DbAnswer("answer", text, sql=sql, columns=columns, rows=[list(r) for r in rows], stats=stats)

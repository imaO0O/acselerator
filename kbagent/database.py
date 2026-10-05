"""Ответы по учётной базе: вопрос → SQL (только чтение) → ответ со ссылкой на запрос.

Модель видит только описание таблиц, а не данные. Сгенерированный запрос
проверяется (один оператор SELECT, без изменения данных) и выполняется
на соединении, открытом только для чтения. Запрос и результат показываются
пользователю — это и есть «источник» ответа.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
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
- Текущая дата: date('now'). Конец текущего месяца: end_of_month(); конец текущего года: end_of_year().
- Выбирай столбцы, нужные для ответа, и всегда добавляй инвентарный или номенклатурный номер, если он есть.
- Не добавляй условий, которых нет в вопросе.
- Если пишешь столбцы с псевдонимом таблицы (e.inv_no), объяви его: FROM equipment e.
- Вычисляемому столбцу (COUNT, SUM) дай понятное название по-русски в двойных кавычках: COUNT(*) AS "всего".
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
Вопрос: Сколько единиц оборудования сейчас в работе?
SQL: SELECT COUNT(*) AS "в работе" FROM equipment WHERE status = 'в работе'
Вопрос: Сколько стоит обед в столовой?
SQL: НЕТ_ДАННЫХ

Вопрос: {question}
SQL:"""

ANSWER_PROMPT = """Ответь на вопрос сотрудника ТОЛЬКО по результату запроса к учётной базе.
Для каждой записи приведи номер, наименование и значения, которые отвечают на вопрос (даты, количество, статус).
Если записей несколько — оформи маркированным списком, по одной записи в строке.
Называй поля так, как они подписаны в заголовке результата.
Пиши полными фразами, не копируй таблицу и разделитель «|». Числа бери только из результата, ничего не складывай.
Числа, даты, номера и ФИО переписывай точно. Если результат пустой — так и скажи, что записей нет.
Если в вопросе предмет назван не так, как в записи (например, спрашивают про мультиметр, а в записи осциллограф), прямо укажи, как он записан в базе.
Если вопрос не о записях учёта (например, о правилах, сроках или порядке действий по регламенту) или строки результата на него не отвечают — ответь ровно одним словом: НЕТ_ДАННЫХ.
В конце ответа поставь ссылку [БД]. Отвечай кратко, по-русски.

Вопрос: {question}

Результат запроса ({count} строк):
{table}"""

# цен и оплаты в учёте нет: такой вопрос к базе не отправляется — небольшая модель на «сколько стоит обед»
# или «сколько стоит поверка» иногда собирает бессмысленный подсчёт по оборудованию
PRICE_RE = re.compile(r"стоит|стоимост|\bцен[аыуе]?\b|расценк|тариф|оплат|зарплат|оклад", re.I)
NOT_RECORDS_RE = re.compile(PRICE_RE.pattern + r"|можно\b|\bнужно\b|"
                            r"\bнадо\b|следует|должн|что делать|порядок|как оформить", re.I)
AGGREGATE_RE = re.compile(r"\b(?:count|sum|avg|min|max|total)\s*\(", re.I)

# где искать похожие записи: наименования, модели, ФИО, места и статусы
NEAR_COLUMNS = ("name", "model", "responsible", "applicant", "location", "inspection_status", "status")
OBJECT_COLUMNS = ("name", "model", "responsible", "applicant")

# поля с наименованиями и ФИО: модель пишет «= 'Белов'», а в базе «Белов И. Н.»
FUZZY_COLUMNS = ("name", "responsible", "applicant", "location", "description", "model")

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
            line = _row_text(cols, [_ru(t, c) for c in cols], row, RECORD_FIELDS | NEXT_FIELDS, question)
            # записи из разных таблиц (оборудование и заявка на него) подписываются таблицей
            lines.append(f"- {t['title'][:1].upper()}{t['title'][1:]}: {line}" if len(found) > 1 else f"- {line}")
    text = "\n".join(lines) if len(lines) > 1 else lines[0][2:]
    sql, columns, rows, t0 = found[0]
    note = _name_mismatch(question, records, columns, rows)
    text = (f"{note}\n\n" if note else "") + text + " [БД]"
    # кратко «что это и в каком состоянии» — дополняет поиск по документам: «партия П-2609-04»
    # сама по себе не похожа на «ЭРИ, не прошедшие входной контроль», а «ЭРИ, карантин» — похожа
    summary = "; ".join(f"{_ru(t, c)} — {v}" for _, cols, rows_, t in found for row in rows_
                        for c, v in zip(cols, row) if c in SUMMARY_FIELDS and v)
    return DbAnswer("answer", text, sql="\n".join(q for q, _, _, _ in found), columns=columns,
                    rows=[list(r) for r in rows], summary=summary, labels=[_ru(t0, c) for c in columns])


@dataclass
class DbAnswer:
    status: str                 # answer | no_data | error
    text: str
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    summary: str = ""            # для записей по номеру: наименование, вид и состояние
    labels: list = field(default_factory=list)   # русские подписи столбцов rows для показа


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
    # «date('end_of_year')», «'end_of_year'», «end_of_year» → end_of_year()
    sql = re.sub(r"date\(\s*'?(end_of_(?:year|month))'?\s*(?:\(\s*\))?\s*\)", r"\1()", sql, flags=re.I)
    sql = re.sub(r"'(end_of_(?:year|month))'", r"\1()", sql, flags=re.I)
    sql = re.sub(r"\b(end_of_(?:year|month))\b(?!\s*\()", r"\1()", sql, flags=re.I)
    sql = re.sub(r"\b(date|datetime)\(\s*'now'(?!\s*,\s*'localtime')", r"\1('now', 'localtime'", sql, flags=re.I)
    return re.sub(r"(?<!date\()\b((?:\w+\.)?created_at)\b(?=\s*(?:between|>=|<=|<|>|=))", r"date(\1)", sql, flags=re.I)


def _fuzzy_text(sql: str) -> str:
    """«responsible = 'Белов'» → «lower(responsible) LIKE '%белов%'»: наименования и ФИО в базе записаны
    полнее, чем в вопросе. Поля со значениями из списка (status и т. п.) не трогаем."""
    cols = "|".join(FUZZY_COLUMNS)
    return re.sub(rf"\b((?:\w+\.)?(?:{cols}))\s*=\s*'([^']*)'",
                  lambda m: f"lower({m.group(1)}) LIKE '%{m.group(2).lower()}%'", sql, flags=re.I)


def _labels(sql: str, columns: list) -> list[str]:
    """Русские подписи столбцов результата: по таблицам, упомянутым в запросе (первая — основная).
    Псевдоним столбца («r.id AS repair_id») подписывается по исходному столбцу."""
    used = sorted((t for t in SCHEMA["tables"] if re.search(rf"\b{t['name']}\b", sql)),
                  key=lambda t: sql.lower().find(t["name"]))
    aliases = {a.lower(): c for c, a in re.findall(r"\b(?:\w+\.)?(\w+)\s+AS\s+\"?(\w+)\"?", sql, re.I)}
    by_alias = {m[2].lower(): m[1].lower() for m in re.finditer(r"\b(?:from|join)\s+(\w+)\s+(?:as\s+)?(\w+)", sql, re.I)}
    tables = {t["name"]: t for t in SCHEMA["tables"]}
    m = re.match(r"\s*select\s+(.+?)\s+from\s", sql, re.I | re.S)
    items = [x.strip() for x in re.split(r",(?![^(]*\))", m.group(1))] if m else []
    out = []
    for k, c in enumerate(columns):
        source = aliases.get(str(c).lower(), c)
        table = None
        if len(items) == len(columns):        # «r.status» — столбец таблицы с псевдонимом r
            ref = re.match(r"(\w+)\.(\w+)", items[k])
            if ref and by_alias.get(ref[1].lower()) in tables:
                table = tables[by_alias[ref[1].lower()]]
        table = table if table and source in table["columns"] else next((t for t in used if source in t["columns"]), None)
        out.append(_ru(table, source) if table else str(c).replace("_", " "))
    return out


GENERIC_WORDS = ("сколько где какой какие есть остаток осталось склад лежит хранится находится номер запись "
                 "отвечает сейчас числится имеется когда следующий прибор приборы просрочен просроченные")
# как сотрудники называют данные учёта — так, как они названы в описании базы для модели
DB_TERMS = [(re.compile(r"\bприбор(?:ы|ов|ам|ами|ах|а|у|ом|е)?\b", re.I), "средства измерений"),
            (re.compile(r"\bпросроч\w*(?:\s+по\s+поверке)?", re.I), "с истёкшим сроком поверки")]


def _near_terms(question: str) -> list[str]:
    """Слова для поиска похожих записей: основы слов (от 4 букв) и фамилии/названия с заглавной буквы целиком."""
    generic = set(tokenize(GENERIC_WORDS + " " + " ".join(
        t["title"] + " " + " ".join(t.get("synonyms", [])) for t in SCHEMA["tables"])))
    terms = [w for w in tokenize(question) if len(w) >= 4 and not w[0].isdigit() and w not in generic]
    for word in re.findall(r"(?<![.!?]\s)(?<!^)\b[А-ЯЁ][а-яё]{3,}\b", question):
        base = word.lower()
        terms.append(base[:-1] if len(base) > 5 and base[-1] in "аеиоуыя" else base)
    return list(dict.fromkeys(terms))


def _near_matches(db_path: Path, question: str) -> list[tuple[str, list, list, dict]]:
    """Записи, в наименованиях, ФИО, местах и статусах которых встречаются слова вопроса."""
    terms = _near_terms(question)
    found = []
    for t in SCHEMA["tables"]:
        cols = [c for c in t["columns"] if c in NEAR_COLUMNS]
        if not cols or not terms:
            continue
        cond = " OR ".join(f"lower({c}) LIKE ?" for c in cols for _ in terms)
        params = tuple(f"%{w}%" for c in cols for w in terms)
        columns, rows = _run(db_path, f"SELECT * FROM {t['name']} WHERE {cond}", params=params)
        if rows:
            shown = f"SELECT * FROM {t['name']} WHERE " + " OR ".join(
                f"lower({c}) LIKE '%{w}%'" for c in cols for w in terms)
            found.append((shown, columns, rows, t))
    return found


def _word_hits(terms: list[str], text: str) -> set[str]:
    """Слова вопроса, с которых начинается слово записи и которые покрывают его почти целиком:
    «припо» — «Припой», «карантин» — «карантина»; но не «контрол» — «Микроконтроллер»
    и не «повер» — «поверхностного»."""
    words = re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))
    return {w for w in terms for word in words if word.startswith(w.replace("ё", "е")) and len(word) <= len(w) + 4}


def _check(sql: str) -> None:
    if not re.match(r"^(select|with)\b", sql, re.I):
        raise ValueError("разрешены только запросы SELECT")
    if ";" in sql or FORBIDDEN.search(sql):
        raise ValueError("запрос изменяет данные или содержит несколько операторов")
    # «SELECT e.inv_no, e.name, COUNT(*) … WHERE …» без GROUP BY: одна случайная строка с общим числом
    m = re.match(r"\s*select\s+(.+?)\s+from\s", sql, re.I | re.S)
    if m and AGGREGATE_RE.search(m.group(1)) and not re.search(r"\bgroup\s+by\b", sql, re.I):
        plain = [x.strip() for x in re.split(r",(?![^(]*\))", m.group(1)) if not AGGREGATE_RE.search(x)]
        if plain:
            raise ValueError(f"столбцы {', '.join(plain)} вместе с COUNT/SUM без GROUP BY: "
                             "оставь только подсчёт или добавь GROUP BY")


def _like(pattern, value, escape=None):
    """LIKE без учёта регистра для кириллицы (встроенный LIKE SQLite знает только латиницу)."""
    if pattern is None or value is None:
        return None
    pattern, value = str(pattern).replace("ё", "е").replace("Ё", "Е"), str(value).replace("ё", "е").replace("Ё", "Е")
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, value, re.IGNORECASE | re.DOTALL) is not None


NUMBER_RE = re.compile(r"\d[\d.,:/-]*\d|\d")


def _unsupported_numbers(text: str, rows: list, question: str) -> list[str]:
    """Числа ответа (даты, количества, номера), которых нет ни в результате запроса, ни в вопросе.
    Так ловятся выдумки вроде «ремонтный срок: в течение 14 дней» или «20 кг припоя» при 12 кг в записи."""
    norm = lambda n: n.strip(".,:/-").lstrip("0") or "0"  # noqa: E731
    whole = {norm(n) for v in [question, *(x for r in rows for x in r)] for n in NUMBER_RE.findall(str(v))}
    parts = {norm(p) for n in whole for p in re.split(r"[.,:/-]", n) if p}   # «2026-09-28» → 2026, 9, 28
    body = re.sub(r"(?m)^\s*\d+[.)]\s", "", re.sub(r"\[БД\]", "", text))     # номера пунктов списка — не данные
    bad = []
    for n in NUMBER_RE.findall(body):
        pieces = [norm(p) for p in re.split(r"[.,:/-]", n.strip(".,:/-")) if p]
        if len(pieces) > 1:                       # дата или время в другом написании: «25.09.2026»
            if not all(p in parts or p in whole for p in pieces):
                bad.append(n)
        elif norm(n) not in whole and not (norm(n).isdigit() and int(norm(n)) <= max(len(rows), 1)):
            bad.append(n)                         # число записей допустимо
    return bad


def _numbers_supported(text: str, rows: list, question: str) -> bool:
    return not _unsupported_numbers(text, rows, question)


def _end_of_month() -> str:
    first_of_next = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    return (first_of_next - timedelta(days=1)).isoformat()


def _run(db_path: Path, sql: str, limit: int = 50, params: tuple = ()) -> tuple[list, list]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        # встроенные lower/upper/LIKE в SQLite не знают кириллицу
        con.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        con.create_function("upper", 1, lambda s: s.upper() if isinstance(s, str) else s, deterministic=True)
        con.create_function("like", 2, _like, deterministic=True)
        con.create_function("like", 3, _like, deterministic=True)
        # «до конца года/месяца»: выражения SQLite с 'start of year' модель путает, функции — нет
        con.create_function("end_of_year", 0, lambda: f"{date.today().year}-12-31")
        con.create_function("end_of_month", 0, _end_of_month)
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


def column_label(column: str) -> str:
    """Подпись столбца без привязки к таблице (для записей, сохранённых до появления подписей)."""
    table = next((t for t in SCHEMA["tables"] if column in t["columns"]), None)
    return _ru(table, column) if table else column


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


DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def display(value) -> str:
    """Значение для показа: даты по-русски (2026-11-13 → 13.11.2026), 12.0 → 12."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return DATE_RE.sub(lambda m: f"{m[3]}.{m[2]}.{m[1]}", str(value))


def _table(columns: list, rows: list) -> str:
    if not rows:
        return "(пусто)"
    lines = [" | ".join(columns)] + [" | ".join("" if v is None else display(v) for v in r) for r in rows]
    return "\n".join(lines)


ID_COLUMNS = ("inv_no", "part_no")
KEY_COLUMNS = ID_COLUMNS + ("name", "applicant", "responsible")   # по ним запись узнаётся в тексте


def _row_text(columns: list, labels: list, row, fields=None, question: str = "", limit: int | None = None) -> str:
    """Запись одной строкой: «ОБ-0302 — Вибростенд: вид — испытательное; участок — …».
    Поля, о которых спрашивают (общие основы слов с вопросом), идут первыми."""
    values = {c: v for c, v in zip(columns, row) if v not in (None, "")}
    head = [display(values[c]) for c in ID_COLUMNS if c in values][:1]
    if "name" in values:
        head.append(display(values["name"]))
    rest, asked = [], set(tokenize(question))
    for c, label, v in zip(columns, labels, row):
        if v in (None, "") or (head and c in ID_COLUMNS + ("name",)) or (fields and c not in fields):
            continue
        if c == "unit" and "qty" in values:
            continue                                   # «количество — 12 кг», а не «12» и «кг» отдельно
        value = f"{display(v)} {values['unit']}" if c == "qty" and "unit" in values else display(v)
        rest.append((f"{label} — {value}", len(asked & set(tokenize(label)))))
    rest = [r for r, _ in sorted(rest, key=lambda x: -x[1])][:limit]
    text = " — ".join(head)
    if rest:
        text = f"{text}: {'; '.join(rest)}" if text else "; ".join(rest)
    return text[:1].upper() + text[1:]


def _plain_answer(columns: list, labels: list, rows: list, question: str = "") -> str:
    """Ответ, собранный прямо из записей, — когда пересказ модели не прошёл проверку."""
    if len(rows) == 1:
        return _row_text(columns, labels, rows[0], question=question)
    # поле с одинаковым значением у всех записей («ответственный — Белов И. Н.») — один раз, в начале
    same = [k for k, c in enumerate(columns) if c not in ID_COLUMNS + ("name", "unit")
            and len({r[k] for r in rows}) == 1 and rows[0][k] not in (None, "")]
    common = "; ".join(f"{labels[k]} — {display(rows[0][k])}" for k in same)
    keep = [k for k in range(len(columns)) if k not in same]
    cols, labs = [columns[k] for k in keep], [labels[k] for k in keep]
    # в списке — главные поля каждой записи; все поля — в таблице под ответом
    lines = [f"- {_row_text(cols, labs, [r[k] for k in keep], question=question, limit=4)}" for r in rows[:15]]
    more = "\n\nПоказаны первые 15 — все записи в таблице ниже." if len(rows) > 15 else ""
    head = f"Найдено записей: {len(rows)}" + (f" ({common})" if common else "") + "."
    return head + "\n\n" + "\n".join(lines) + more


def _row_keys(columns: list, row) -> list[str]:
    """Чем запись должна быть названа в пересказе: номер, иначе наименование, иначе все значения (итоги)."""
    for c in KEY_COLUMNS:
        if c in columns and row[columns.index(c)] not in (None, ""):
            return [display(row[columns.index(c)])]
    return [display(v) for v in row if v not in (None, "")]


def _mentions(text: str, value: str) -> bool:
    norm = lambda x: re.sub(r"\s+", " ", x.lower().replace("ё", "е"))  # noqa: E731
    value = norm(value)
    if re.fullmatch(r"[\d.,]+", value):
        return re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d])", norm(text)) is not None
    return value in norm(text)


EMPTY_CLAIM_RE = re.compile(r"^\W*(?:нет\b|записей нет|ничего не найдено)|записей нет|нет данных|не найден[оаы]?\b|"
                            r"не числится|отсутству", re.I)


def _retelling_problem(text: str, columns: list, labels: list, rows: list) -> str | None:
    """Почему пересказ записей нельзя показывать (или None): «Записей нет» при найденных записях,
    скопированная таблица, пропущенная запись, разбор одной записи по полям."""
    if not rows:
        return None
    if "|" in text:
        return "таблица"
    for row in rows[:10]:
        values = [display(v) for v in row if v not in (None, "")][:3]
        if len(values) == 3 and _mentions(text, ", ".join(values)):
            return "таблица"
    if EMPTY_CLAIM_RE.search(text):
        return "записей нет"
    if len(rows) == 1:
        keys = [display(rows[0][columns.index(c)]) for c in KEY_COLUMNS
                if c in columns and rows[0][columns.index(c)] not in (None, "")] or _row_keys(columns, rows[0])
        if not any(_mentions(text, key) for key in keys):
            return "пропущена запись"
    for row in rows[:10]:
        if len(rows) > 1 and not all(_mentions(text, key) for key in _row_keys(columns, row)):
            return "пропущена запись"
    field_marks = sum(1 for lab in set(labels) if re.search(rf"\b{re.escape(lab)}\s*:", text, re.I))
    if len(rows) == 1 and field_marks >= 3:
        return "поля списком"      # «номер заявки: 2; инвентарный номер: …» — разбор записи по полям
    return None


def _answer_near(question: str, db_path: Path, stats: dict) -> "DbAnswer | None":
    """Запрос не нашёл записей — ищем похожие по словам вопроса: «флюс» лежит на складе, а не в оборудовании;
    «ПОС-40» нет, но есть «ПОС-61»."""
    found = _near_matches(db_path, question)
    if not found:
        return None
    # обозначения из вопроса («ПОС-40», «DB-9», «100 нФ»): если их нет в записи — это не то, что спрашивали
    codes = [c.lower().replace(" ", "") for c in re.findall(r"[A-Za-zА-Яа-яЁё]*-?\s?\d+[A-Za-zА-Яа-я\d/-]*", question)]
    terms = _near_terms(question)
    scored = []
    for _, cols, rows, t in found:
        for row in rows:
            # совпадение в наименовании, модели, ФИО — это сам предмет вопроса; в месте или статусе — признак
            obj = _word_hits(terms, " ".join(str(v) for c, v in zip(cols, row) if c in OBJECT_COLUMNS))
            attr = _word_hits(terms, " ".join(str(v) for c, v in zip(cols, row) if c in NEAR_COLUMNS)) - obj
            if obj or attr:
                scored.append(((len(obj), len(attr)), obj | attr, obj, row, cols, t))
    if not scored:
        return None
    best = max(s[0] for s in scored)
    known_obj = set().union(*(s[2] for s in scored))      # какие предметы вопроса вообще есть в учёте
    known = set().union(*(s[1] for s in scored))
    scored = [s for s in scored if s[0] == best]          # «штангенциркуль» важнее «поверки» в статусе
    lines, exact = [], False
    for _, matched, obj, row, cols, t in scored:
        record = " ".join(str(v) for v in row).lower().replace("ё", "е").replace(" ", "")
        found_all = obj == known_obj if known_obj else matched == known
        exact = exact or (found_all and all(c.replace("ё", "е") in record for c in codes))
        lines.append(f"- {_row_text(cols, [_ru(t, c) for c in cols], row, RECORD_FIELDS)}")
    lead = "Найдено в учёте:" if exact else "Точного совпадения в учёте нет. Похожие записи:"
    text = lead + "\n" + "\n".join(lines[:10]) + " [БД]"
    t, cols = scored[0][5], scored[0][4]
    rows = [s[3] for s in scored if s[5] is t]
    return DbAnswer("answer", text, sql="\n".join(q for q, _, _, _ in found), columns=cols,
                    rows=[list(r) for r in rows], stats=stats, labels=[_ru(t, c) for c in cols])


# поля записи, которые показываются в ответе по похожим записям
RECORD_FIELDS = {"inv_no", "part_no", "id", "name", "model", "qty", "unit", "batch", "location", "status",
                 "inspection_status", "responsible", "applicant", "next_check", "category", "description"}
NEXT_FIELDS = {"last_check", "check_type", "received", "created_at"}   # для записи по номеру — тоже


def answer_from_database(question: str, db_path: Path, records: list[str] | None = None,
                         near: bool = False) -> DbAnswer:
    """records — номера записей из вопроса «запись + правило» («СИ-0012 просрочен — можно ли…»).
    Тогда запись ищется только по номеру, без условий из вопроса: посылка сотрудника может быть
    неверной (прибор не просрочен или это вовсе не мультиметр), и это как раз нужно показать.
    near — при пустом результате искать похожие записи (только когда вопрос явно к базе)."""
    stats = {}
    if records:
        return _answer_records(question, db_path, records)
    if PRICE_RE.search(question):
        return DbAnswer("no_data", "В учётной базе нет цен и оплаты — только оборудование, средства измерений, "
                                   "заявки на ремонт и склад.", stats=stats)
    sql_question = question
    for rx, term in DB_TERMS:
        sql_question = rx.sub(term, sql_question)
    try:
        raw, s1 = llm.chat([{"role": "user", "content": SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION,
                                                                          question=sql_question)}],
                           max_tokens=300)
    except llm.ModelError as e:
        return DbAnswer("error", f"Языковая модель недоступна: {e}")
    stats["sql_seconds"] = s1["seconds"]
    if "НЕТ_ДАННЫХ" in raw:
        return DbAnswer("no_data", "В учётной базе нет данных для ответа на этот вопрос.", stats=stats)

    sql = _fuzzy_text(_fix_dates(_repair_aliases(_extract_sql(raw))))
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
            fix = SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION, question=sql_question) + f"\n{sql}\n\nОшибка: {error}\nИсправленный SQL:"
            raw, _ = llm.chat([{"role": "user", "content": fix}], max_tokens=300)
            sql = _fuzzy_text(_fix_dates(_repair_aliases(_extract_sql(raw))))

    # похожие записи — только если модель искала запись по наименованию, ФИО или месту и не нашла;
    # на вопросах «до конца года» поиск по словам ловит случайные совпадения («повер» в «поверхностного»)
    # о ценах, правилах и порядке действий в учёте нет записей — похожие записи тут не ответ
    if not rows and near and not NOT_RECORDS_RE.search(question):
        try:
            near_answer = _answer_near(question, db_path, stats)
        except (sqlite3.Error, llm.ModelError):
            near_answer = None
        if near_answer:
            return near_answer
    labels = _labels(sql, columns)
    text, s2 = llm.chat([{"role": "user", "content": ANSWER_PROMPT.format(
        question=question, count=len(rows), table=_table(labels, rows))}], max_tokens=400)
    stats.update(answer_seconds=s2["seconds"], tokens_per_second=s2.get("tokens_per_second"))
    if "НЕТ_ДАННЫХ" in text:
        return DbAnswer("no_data", "Записи учёта не отвечают на этот вопрос.", sql=sql, columns=columns,
                        rows=[list(r) for r in rows], stats=stats)
    bad = _unsupported_numbers(text, rows, question)
    problem = _retelling_problem(text, columns, labels, rows)
    if len(rows) > 1 and not problem:
        problem = "несколько записей"   # модель перечисляет их строками через запятую, без подписей полей
    keyed = any(c in columns for c in KEY_COLUMNS)
    total = len(rows) == 1 and not keyed and not any(re.search(r"[a-z]", lab, re.I) for lab in labels)
    if (bad or problem) and (keyed or total):
        # у записей есть номер или наименование, а у итога — понятные подписи: ответ собирается прямо
        # из результата — точно, единообразно и без второго вызова модели («Партия: 1» при трёх партиях)
        stats["retelling_replaced"] = problem or "числа"
        text, bad = _plain_answer(columns, labels, rows, question), []
    if bad:
        # одна попытка исправиться: модель видит, каких чисел нет в записях
        messages = [{"role": "user", "content": ANSWER_PROMPT.format(question=question, count=len(rows),
                                                                      table=_table(labels, rows))},
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"В ответе есть числа, которых нет в результате запроса: "
                                                f"{', '.join(bad)}. Перепиши ответ, используя только значения "
                                                "из таблицы, ничего не складывая и не придумывая."}]
        text, _ = llm.chat(messages, max_tokens=400)
        if _unsupported_numbers(text, rows, question) or "НЕТ_ДАННЫХ" in text:
            stats["unsupported_numbers"] = True
            return DbAnswer("no_data", "Ответ по учётной базе не подтвердился записями.", sql=sql, columns=columns,
                            rows=[list(r) for r in rows], stats=stats)
    if len(re.findall(r"(?m)^\s*\d+[.)]\s", text)) == 1:
        text = re.sub(r"(?m)^(\s*)1[.)]\s+", r"\1", text).strip()
        text = text[:1].upper() + text[1:]
    text = re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"\s*\[БД\]\s*", " ", text)).strip() + " [БД]"
    return DbAnswer("answer", text, sql=sql, columns=columns, rows=[list(r) for r in rows], stats=stats,
                    labels=labels)

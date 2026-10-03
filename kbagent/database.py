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

from . import llm

SCHEMA_DESCRIPTION = """Таблицы учётной базы предприятия (SQLite):

equipment — технологическое оборудование
  inv_no — инвентарный номер (например, 'ОБ-0102'); name — наименование; kind — вид (технологическое, контрольное, испытательное, вспомогательное);
  location — участок; status — состояние ('в работе', 'в ремонте', 'законсервировано'); responsible — ответственный (ФИО)

measuring_instruments — средства измерений (СИ)
  inv_no — инвентарный номер (например, 'СИ-0012'); name — наименование; model — модель; serial_no — заводской номер; location — где находится;
  last_check — дата последней поверки или калибровки (ГГГГ-ММ-ДД); next_check — дата очередной поверки или калибровки (ГГГГ-ММ-ДД);
  check_type — 'поверка' или 'калибровка'; status — 'в эксплуатации', 'в поверке', 'не поверен'

repair_requests — заявки на ремонт оборудования
  id — номер заявки; inv_no — инвентарный номер оборудования (связь с equipment.inv_no); created_at — дата и время подачи (ГГГГ-ММ-ДД ЧЧ:ММ);
  category — категория отказа ('А' — угроза жизни и здоровью, 'Б' — остановка участка, 'В' — частичная потеря функций);
  description — описание неисправности; status — 'новая', 'в работе', 'выполнена'; applicant — заявитель

stock — остатки на складе
  part_no — номенклатурный номер; name — наименование; kind — 'ЭРИ', 'комплектующие', 'материалы'; qty — количество; unit — единица;
  batch — номер партии; received — дата поступления; inspection_status — статус входного контроля ('годен', 'карантин', 'брак'); location — место хранения
"""

SQL_PROMPT = """Ты переводишь вопрос сотрудника в один SQL-запрос SQLite к учётной базе.

{schema}
Правила:
- Только один оператор SELECT. Никаких INSERT, UPDATE, DELETE, DROP, PRAGMA.
- Для поиска по наименованию используй lower(name) LIKE '%...%' по основе слова (например, '%осциллограф%').
- Текущая дата: date('now').
- Выбирай столбцы, нужные для ответа, и всегда добавляй инвентарный или номенклатурный номер, если он есть.
- Не добавляй условий, которых нет в вопросе.
- Если на вопрос нельзя ответить по этим таблицам, верни ровно: НЕТ_ДАННЫХ
Верни только SQL без пояснений.

Примеры:
Вопрос: У каких средств измерений истёк срок поверки?
SQL: SELECT inv_no, name, model, next_check, status FROM measuring_instruments WHERE next_check < date('now') ORDER BY next_check
Вопрос: Сколько микроконтроллеров годно к выдаче?
SQL: SELECT part_no, name, qty, unit, batch FROM stock WHERE lower(name) LIKE '%микроконтроллер%' AND inspection_status = 'годен'
Вопрос: Есть ли на складе конденсаторы 100 нФ?
SQL: SELECT part_no, name, qty, unit, batch, inspection_status FROM stock WHERE lower(name) LIKE '%конденсатор%' AND lower(name) LIKE '%100 нф%'
Вопрос: Какие партии сейчас на входном контроле?
SQL: SELECT part_no, name, qty, unit, batch, received FROM stock WHERE inspection_status = 'карантин'
Вопрос: Какие заявки на ремонт категории А открыты?
SQL: SELECT r.id, r.inv_no, e.name, r.created_at, r.description, r.status FROM repair_requests r JOIN equipment e ON e.inv_no = r.inv_no WHERE r.category = 'А' AND r.status <> 'выполнена'
Вопрос: Сколько стоит обед в столовой?
SQL: НЕТ_ДАННЫХ

Вопрос: {question}
SQL:"""

ANSWER_PROMPT = """Ответь на вопрос сотрудника ТОЛЬКО по результату запроса к учётной базе.
Для каждой записи приведи номер, наименование и значения, которые отвечают на вопрос (даты, количество, статус).
Если записей несколько — оформи маркированным списком, по одной записи в строке.
Числа, даты, номера и ФИО переписывай точно. Если результат пустой — так и скажи, что записей нет.
В конце ответа поставь ссылку [БД]. Отвечай кратко, по-русски.

Вопрос: {question}

Результат запроса ({count} строк):
{table}"""

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b", re.I)


@dataclass
class DbAnswer:
    status: str                 # answer | no_data | error
    text: str
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    return sql


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


def _run(db_path: Path, sql: str, limit: int = 50) -> tuple[list, list]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        # встроенные lower/upper/LIKE в SQLite не знают кириллицу
        con.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
        con.create_function("upper", 1, lambda s: s.upper() if isinstance(s, str) else s, deterministic=True)
        con.create_function("like", 2, _like, deterministic=True)
        con.create_function("like", 3, _like, deterministic=True)
        con.execute("PRAGMA query_only = ON")
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchmany(limit)
    finally:
        con.close()


def _table(columns: list, rows: list) -> str:
    if not rows:
        return "(пусто)"
    lines = [" | ".join(columns)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    return "\n".join(lines)


def answer_from_database(question: str, db_path: Path) -> DbAnswer:
    stats = {}
    try:
        raw, s1 = llm.chat([{"role": "user", "content": SQL_PROMPT.format(schema=SCHEMA_DESCRIPTION, question=question)}],
                           max_tokens=300)
    except llm.ModelError as e:
        return DbAnswer("error", f"Языковая модель недоступна: {e}")
    stats["sql_seconds"] = s1["seconds"]
    if "НЕТ_ДАННЫХ" in raw:
        return DbAnswer("no_data", "В учётной базе нет данных для ответа на этот вопрос.", stats=stats)

    sql = _extract_sql(raw)
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
            sql = _extract_sql(raw)

    text, s2 = llm.chat([{"role": "user", "content": ANSWER_PROMPT.format(
        question=question, count=len(rows), table=_table(columns, rows))}], max_tokens=400)
    stats.update(answer_seconds=s2["seconds"], tokens_per_second=s2.get("tokens_per_second"))
    text = re.sub(r"\s*\[БД\]\s*", " ", text).strip() + " [БД]"
    return DbAnswer("answer", text, sql=sql, columns=columns, rows=[list(r) for r in rows], stats=stats)

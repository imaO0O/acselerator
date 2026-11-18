"""Черновик справочника параметров из руководства по эксплуатации — для проверки экспертом.

Справочник заполняется вручную: у партнёра сотни приборов, и это главный порог масштабирования ТКП. Программа
сама находит в тексте руководства характеристики, которые справочник уже знает (полоса пропускания, число каналов,
пределы измерений, масса…), и предлагает по каждой модели одно значение — с единицей, условием («не более»),
страницей и цитатой; рядом — другие значения, встреченные в тексте, чтобы неоднозначность была видна сразу.
В справочник попадает только то, что эксперт отметил и при необходимости исправил, — через ту же проверку,
что и загружаемый файл.

Как читается текст (без языковой модели):
  - строка таблицы, начинающаяся с модели: «АКИП-4122/2(V) 100 МГц 500 МГц» — полоса и дискретизация;
  - столбцы таблицы по моделям: «Полоса пропускания 100МГц 200МГц» при двух моделях — по порядку;
  - пределы измерений мультиметра: наибольшее значение в таблице предела («200 мВ / 2 В / … / 1000 В»);
  - «Число каналов вертикального отклонения – 2», «Температурный диапазон 80–500 °C», «Вес около 160 гр.»
Проверка на демо (scripts/draft_spravochnik.py --check): из 104 значений, внесённых вручную по 11 руководствам,
программа сама находит 86 (83 %), из 94 предложенных значений верны 86. Таблицы, которые при извлечении текста из PDF «рассыпаются» (общие ячейки на
несколько моделей), не угадываются — поэтому это черновик, а не справочник.
"""
import csv
import io
import re
from collections import defaultdict

from . import spravochnik as S

PARAMS = {   # параметр → (величина, название в тексте, как выбрать значение)
    "Полоса пропускания": ("частота", r"полос\w*\s+пропускани\w*", "one"),
    "Частота дискретизации на канал": ("дискретизация", r"(?:частот\w*\s+)?дискретизаци\w*", "one"),
    "Время нарастания": ("время", r"нарастани\w*", "one"),
    "Максимальное входное напряжение": ("напряжение", r"(?:максимальн\w*\s+)?входн\w*\s+напряжени\w*|"
                                                       r"диапазоне\s+амплитуд", "near_max"),
    "Число каналов": ("количество", r"(?:числ\w*|количеств\w*)\s+канал\w*|канал\w*\s+вертикальн\w*", "count"),
    "Постоянное напряжение, верхний предел": ("напряжение", r"постоянн\w*\s+напряжени\w*|\bDCV\b|\bDC\s*V\b", "max"),
    "Переменное напряжение, верхний предел": ("напряжение", r"переменн\w*\s+напряжени\w*|\bACV\b|\bAC\s*V\b", "max"),
    "Постоянный ток, верхний предел": ("ток", r"постоянн\w*\s+ток\w*|\bDCA\b|\bDC\s*A\b", "max"),
    "Переменный ток, верхний предел": ("ток", r"переменн\w*\s+ток\w*|\bACA\b|\bAC\s*A\b", "max"),
    "Сопротивление, верхний предел": ("сопротивление", r"сопротивлени\w*(?!\s+изоляц)|\bΩ\b", "max"),
    "Масса": ("масса", r"\bмасс\w*|\bвес\b", "one"),
    "Потребляемая мощность": ("мощность", r"потребля\w*\s+мощност\w*", "one"),
    "Температура жала паяльника": ("температура", r"температурн\w*\s+диапазон|температур\w*\s+(?:жала|паяльник)",
                                   "range"),
}
KIND_PARAMS = {
    "Осциллограф": ["Полоса пропускания", "Число каналов", "Частота дискретизации на канал", "Время нарастания",
                    "Максимальное входное напряжение", "Масса"],
    "Мультиметр": ["Постоянное напряжение, верхний предел", "Переменное напряжение, верхний предел",
                   "Постоянный ток, верхний предел", "Переменный ток, верхний предел", "Сопротивление, верхний предел",
                   "Масса"],
    "Паяльная станция": ["Температура жала паяльника", "Потребляемая мощность", "Масса"],
}
UNIT = (r"ГГц|МГц|кГц|Гц|Гвыб/с|Мвыб/с|ГС/с|МС/с|кВ|мВ|Впик|В|мкА|мА|А|МОм|MΩ|кОм|kΩ|Ом|Ω|°\s?[CС]|℃|кВт|Вт|пс|нс|"
        r"мкс|мс|кг|гр\.?|г")
VAL_RE = re.compile(rf"({S.NUM})\s*(?:(?:–|-|—|…|~|до)\s*({S.NUM})\s*)?({UNIT})(?![А-Яа-яA-Za-z])")
LESS_RE = re.compile(r"≤|не\s+более|не\s+больше|<")
MORE_RE = re.compile(r"≥|не\s+менее|не\s+меньше|>")
UNIT_FIX = {"°С": "°C", "℃": "°C", "ГС/с": "Гвыб/с", "МС/с": "Мвыб/с", "Впик": "В", "MΩ": "МОм", "kΩ": "кОм",
            "Ω": "Ом", "гр": "г", "гр.": "г"}
MODEL_RANK, COLUMNS_RANK, LIMIT_RANK, PLAIN_RANK = 4, 3, 2, 1      # чем надёжнее способ, тем выше


def _unit(u: str) -> str:
    u = u.replace(" ", "")
    return UNIT_FIX.get(u, u)


def _values(window: str, dim: str) -> list[tuple]:
    """(число, второе число диапазона, единица, начало, конец) — только единицы нужной величины."""
    out = []
    for v in VAL_RE.finditer(window):
        u = _unit(v.group(3))
        if u in S.UNITS and S.UNITS[u][0] == dim:
            out.append((v.group(1), v.group(2), u, v.start(), v.end()))
    return out


def _first_run(vals: list[tuple], window: str) -> list[tuple]:
    """Подряд идущие значения — столбцы таблицы: между ними только пробелы, «≤», «/»."""
    run = vals[:1]
    for a, b in zip(vals, vals[1:]):
        if not re.fullmatch(r"[\s≤≥<>/,;±]*", window[a[4]:b[3]]):
            break
        run.append(b)
    return run


def kind_of(title: str) -> str | None:
    return next((k for k, rx in S.KINDS.items() if re.search(rx, title, re.I)), None)


def models_from_title(title: str) -> list[str]:
    """«Осциллографы цифровые АКИП-4143/1, 4143/1А, 4143/2» → АКИП-4143/1, АКИП-4143/1А, АКИП-4143/2."""
    out, brand = [], ""
    for t in S.MODEL_TOKEN_RE.findall(title):
        t = t.rstrip(".,;)")
        if m := re.match(r"([A-Za-zА-Яа-яЁё]+-?)\d", t):
            brand = m.group(1)
        elif brand and t[:1].isdigit():
            t = brand + t
        if t not in out:
            out.append(t)
    return out


def candidates(chunks, models: list[str], kind: str | None, params: list[str] | None = None) -> list[dict]:
    """Строки черновика: по одной на модель и параметр. chunks — фрагменты одного руководства по порядку страниц
    (Chunk или dict с page_start и text)."""
    params = [p for p in (params or KIND_PARAMS.get(kind or "", list(PARAMS))) if p in PARAMS]
    best: dict[tuple, dict] = {}
    seen: dict[tuple, dict] = defaultdict(dict)          # (модель, параметр) → значение → страница

    def add(model, param, val, cond, page, quote, rank):
        a, b, u = val[0], val[1], val[2]
        value = f"{a}–{b}" if b else a
        key = (model, param)
        seen[key].setdefault(f"{value} {u}".strip(), page)
        if key not in best or rank > best[key]["rank"]:   # при равной надёжности — первое по тексту
            best[key] = {"Модель": model, "Вид": kind or "", "Параметр": param, "Значение": value, "Единица": u,
                         "Условие": cond, "Страница": page, "Цитата": re.sub(r"\s+", " ", quote).strip()[:180],
                         "rank": rank}

    for c in chunks:
        raw, page = (c["text"], c["page_start"]) if isinstance(c, dict) else (c.text, c.page_start)
        if not page:
            continue
        text = S.normalize(raw)
        # строка таблицы, начинающаяся с модели: «АКИП-4122/2(V) 100 МГц 500 МГц / 1 ГГц»
        for model in models:
            for m in re.finditer(re.escape(model) + r"(?![0-9A-Za-zА-Яа-я/])", text, re.I):
                tail = text[m.end(): m.end() + 80]
                run = _first_run(_values(tail, "частота"), tail)
                if run and run[0][3] < 4 and "Полоса пропускания" in params:
                    add(model, "Полоса пропускания", run[0], "", page, model + tail, MODEL_RANK)
                    if len(run) > 1 and "Частота дискретизации на канал" in params:
                        mega = S._parse_number(run[1][0]) * S.UNITS[run[1][2]][1] / 1e6
                        add(model, "Частота дискретизации на канал", (f"{mega:g}".replace(".", ","), None, "Мвыб/с"),
                            "", page, model + tail, MODEL_RANK)
        for param in params:
            dim, alias, how = PARAMS[param]
            for m in re.finditer(alias, text, re.I):
                window = text[m.end(): m.end() + (700 if how == "max" else 160)]
                if how != "max":                              # таблица предела — длинная, остальное — до конца фразы
                    window = re.split(r"(?<=[.;])\s+[А-ЯA-Z][а-яa-z]", window)[0]
                table = False
                if how == "count":
                    n = re.match(r"\D{0,40}?(\d{1,2})(?!\d|[.,]\d)", window)
                    vals = [(n.group(1), None, "", n.start(1), n.end(1))] if n else []
                    # «Число каналов вертикального отклонения – 2» — табличная запись надёжнее упоминания в тексте
                    table = bool(n and re.search(r"[–:-]\s*$", window[:n.start(1)]))
                else:
                    vals = _values(window, dim)
                if not vals:
                    continue
                before = text[max(0, m.start() - 40): m.end()] + window[: vals[0][3]]
                cond = "не более" if LESS_RE.search(before) else "не менее" if MORE_RE.search(before) else ""
                around = S._norm(text[max(0, m.start() - 200): m.end() + 160])
                named = [x for x in models if S._norm(x) in around]
                quote = text[m.start(): m.end()] + window[: vals[min(len(vals), 6) - 1][4]]
                if how in ("max", "near_max"):
                    top = max(vals, key=lambda x: S._parse_number(x[0]) * S.UNITS[x[2]][1])
                    for x in models:
                        add(x, param, top, cond, page, quote, LIMIT_RANK)
                elif how == "count":
                    for x in models:
                        add(x, param, vals[0], cond, page, quote, LIMIT_RANK if table else PLAIN_RANK)
                elif how == "range":
                    if rng := next((x for x in vals if x[1]), None):
                        for x in models:
                            add(x, param, rng, cond, page, quote, LIMIT_RANK)
                else:
                    run = _first_run(vals, window)
                    if len(run) == len(models) > 1:
                        for x, v in zip(models, run):                 # столбцы таблицы по моделям
                            add(x, param, v, cond, page, quote, COLUMNS_RANK)
                    elif named and len(run) == 1:
                        for x in named:
                            add(x, param, run[0], cond, page, quote, COLUMNS_RANK)
                    elif len(models) == 1 or len(run) == 1:
                        for x in models:
                            add(x, param, run[0], cond, page, quote, PLAIN_RANK)
    out = []
    for key, row in best.items():
        shown = f"{row['Значение']} {row['Единица']}".strip()
        others = [f"{v} (с. {p})" for v, p in seen[key].items() if v != shown]
        row["Другие значения в тексте"] = "; ".join(others[:5])
        row.pop("rank")
        out.append(row)
    order = {p: i for i, p in enumerate(params)}
    return sorted(out, key=lambda r: (models.index(r["Модель"]), order.get(r["Параметр"], 99)))


def add_rows(rows: list[dict], doc_id: str) -> "S.Report":
    """Отмеченные экспертом строки — в справочник через ту же проверку, что и загружаемый файл; повторы не
    дублируются, значения, расходящиеся с уже внесёнными, станут противоречием для эксперта."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(S.COLUMNS)
    if S.PATH.exists():
        with open(S.PATH, encoding="utf-8-sig", newline="") as f:
            current = list(csv.DictReader(f, delimiter=";"))
        w.writerows([r.get(c, "") for c in S.COLUMNS] for r in current)
    for r in rows:
        w.writerow([r["Модель"], r.get("Вид", ""), r["Параметр"], r["Значение"], r.get("Единица", ""),
                    r.get("Условие", ""), doc_id, r["Страница"], r.get("Цитата", "")])
    return S.replace_file("pribory.csv", buf.getvalue().encode("utf-8-sig"))


def check(chunks_by_doc: dict, values: list) -> dict:
    """Сверка с уже заполненным справочником: сколько внесённых вручную значений программа нашла сама."""
    truth = defaultdict(list)
    for v in values:
        if v.param in PARAMS:
            truth[v.doc].append(v)
    total = found = proposed = 0
    for doc, items in truth.items():
        models = list(dict.fromkeys(v.model for v in items))
        params = list(dict.fromkeys(v.param for v in items))
        got = {(r["Модель"], r["Параметр"]): r for r in candidates(chunks_by_doc.get(doc, []), models,
                                                                    items[0].kind, params)}
        proposed += len(got)
        for v in items:
            r = got.get((v.model, v.param))
            total += 1
            if r and _key(r["Значение"], r["Единица"]) == _key(v.text, v.unit):
                found += 1
    return {"total": total, "found": found, "proposed": proposed, "docs": len(truth)}


def _key(value: str, unit: str) -> tuple:
    mult = S.UNITS.get(S.UNIT_ALIASES.get(unit, unit), ("", 1))[1]
    return tuple(round(S._parse_number(n) * mult, 9) for n in re.findall(S.NUM, value))

"""Цены и остатки из 1С в прайс для ТКП — только чтение, через стандартный интерфейс OData.

Конфигурации: «Бухгалтерия предприятия 3.0» (KB_1C_CONFIG=bp, по умолчанию) и «Управление торговлей 11»,
«Комплексная автоматизация 2», ERP (KB_1C_CONFIG=ut). Читается:
  - справочник «Номенклатура» — наименование, полное наименование, артикул;
  - регистр «Цены номенклатуры» — последняя цена выбранного типа (вида) цен, KB_1C_PRICE_TYPE, например «Оптовая»;
  - остатки (KB_1C_STOCK=1): в БП — количество на счёте 41.01, в УТ — регистр «Товары на складах», «В наличии».

Номенклатура сопоставляется с моделями справочника параметров по артикулу или наименованию: «Осциллограф
цифровой АКИП-4143/1» → АКИП-4143/1, но не АКИП-4143/1А. Позиция, подходящая сразу к двум моделям, не
загружается. Цены записываются в прайс (data/spravochnik/prajs.csv) с той же проверкой, что и файл от
администратора: пропуски, цены, разные цены одной модели. Цена в валюте не подставляется. Срок поставки — «в
наличии», если есть остаток, иначе — как был в прайсе (его ведёт менеджер).

Подключение: KB_1C_URL — адрес стандартного интерфейса OData (http://сервер/база/odata/standard.odata),
KB_1C_USER — пользователь 1С с правом только на чтение; пароль — KB_1C_PASSWORD или data/1c_password.txt (в git
и резервную копию не попадает). В 1С в состав стандартного интерфейса OData включаются: справочники
«Номенклатура», «Типы цен номенклатуры» (в УТ — «Виды цен»), «Валюты», регистр «Цены номенклатуры»; для
остатков — «План счетов Хозрасчетный» и регистр «Хозрасчетный» (БП) или «Товары на складах» (УТ).
Только GET-запросы — в 1С ничего не меняется. Обновление — scripts/sync_1c.py --watch 3600 или кнопка
«Обновить цены из 1С» в «Справочник» → «Прайс для ТКП».
"""
import csv
import io
import json
import os
import re
import time
from datetime import date

import requests

from . import prajs
from . import spravochnik as spr
from .config import ROOT
from .text import designation_tokens

PASSWORD_FILE = ROOT / "data" / "1c_password.txt"
REPORT = spr.DIR / "prajs_1c.json"
EMPTY = "00000000-0000-0000-0000-000000000000"
PAGE = 5000
RUB = "643"
PRESETS = {
    "bp": {"title": "Бухгалтерия предприятия 3.0", "prices": "InformationRegister_ЦеныНоменклатуры",
           "price_type_field": "ТипЦен_Key", "price_types": "Catalog_ТипыЦенНоменклатуры",
           "stock": "AccountingRegister_Хозрасчетный/Balance()", "stock_item": "ExtDimension1",
           "stock_qty": "КоличествоBalance", "stock_account": "41.01"},
    "ut": {"title": "Управление торговлей 11, КА 2, ERP", "prices": "InformationRegister_ЦеныНоменклатуры",
           "price_type_field": "ВидЦены_Key", "price_types": "Catalog_ВидыЦен",
           "stock": "AccumulationRegister_ТоварыНаСкладах/Balance()", "stock_item": "Номенклатура_Key",
           "stock_qty": "ВНаличииBalance", "stock_account": ""},
}


class OneCError(Exception):
    """1С недоступна или объект не опубликован — понятной фразой для администратора."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def conf() -> dict:
    preset = PRESETS.get(_env("KB_1C_CONFIG", "bp").lower(), PRESETS["bp"])
    return {**preset, "url": _env("KB_1C_URL").rstrip("/"), "user": _env("KB_1C_USER"),
            "price_type": _env("KB_1C_PRICE_TYPE"), "stock_on": _env("KB_1C_STOCK", "0") in ("1", "true", "да")}


def configured() -> bool:
    c = conf()
    return bool(c["url"] and c["user"] and c["price_type"])


def _password() -> str:
    value = os.environ.get("KB_1C_PASSWORD")
    if value is None and PASSWORD_FILE.exists():
        value = PASSWORD_FILE.read_text(encoding="utf-8")
    return (value or "").strip()


def _get(c: dict, path: str, params: dict | None = None, session=None) -> list[dict]:
    """GET к OData с постраничной выборкой; ошибки — фразой, понятной администратору."""
    http = session or requests
    out, skip = [], 0
    while True:
        query = {"$format": "json", "$top": PAGE, "$skip": skip, **(params or {})}
        try:
            r = http.get(f"{c['url']}/{path}", params=query, auth=(c["user"], _password()), timeout=60)
        except requests.RequestException as e:
            raise OneCError(f"1С не отвечает по адресу {c['url']}: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise OneCError("1С отклонила вход: проверьте пользователя и пароль (KB_1C_USER, data/1c_password.txt) "
                            "и права пользователя на чтение")
        if r.status_code == 404:
            obj = path.split("/")[0].split("(")[0]
            raise OneCError(f"В 1С не опубликован «{obj}»: включите его в состав стандартного интерфейса OData")
        if r.status_code >= 400:
            raise OneCError(f"1С вернула ошибку {r.status_code} на «{path}»")
        try:
            page = r.json().get("value", [])
        except ValueError as e:
            raise OneCError("1С вернула не JSON — проверьте адрес: он должен заканчиваться на /odata/standard.odata") from e
        out += page
        if len(page) < PAGE:
            return out
        skip += PAGE


def _guid(value: str) -> str:
    return f"guid'{value}'"


# ---------- сопоставление номенклатуры 1С с моделями справочника ----------

def _whole_code(model: str) -> str | None:
    """«АКИП-4143/1» → «code:akип41431» — обозначение модели целиком, как его видит поиск."""
    codes = [t for t in designation_tokens(model) if t.startswith("code:")]
    return max(codes, key=len) if codes else None


def matcher(models: set[str]):
    """Функция «строка номенклатуры → модель справочника или None»; две модели сразу — неоднозначно (None, список)."""
    by_norm = {spr._norm(m): m for m in models}
    by_code = {}
    for m in models:
        if code := _whole_code(m):
            by_code.setdefault(code, set()).add(m)
    plain = [(m, re.compile(r"(?<![0-9A-ZА-Я])" + re.escape(spr._norm(m)) + r"(?![0-9A-ZА-Я])"))
             for m in models if not _whole_code(m)]

    def match(*names: str) -> tuple[str | None, list[str]]:
        names = [n for n in names if n and n.strip()]
        for n in names:                                        # артикул или наименование — ровно модель
            if (m := by_norm.get(spr._norm(n))) is not None:
                return m, []
        found = set()
        for n in names:
            for t in designation_tokens(n):
                found |= by_code.get(t, set())
            norm = spr._norm(n)
            found |= {m for m, rx in plain if rx.search(norm)}
        if len(found) == 1:
            return found.pop(), []
        return None, sorted(found)
    return match


# ---------- чтение 1С ----------

def fetch(c: dict | None = None, session=None) -> dict:
    """Номенклатура, цены выбранного типа и остатки из 1С — как есть, без сопоставления."""
    c = c or conf()
    types = _get(c, c["price_types"], {"$select": "Ref_Key,Description,ЦенаВключаетНДС"}, session)
    ptype = next((t for t in types if t.get("Description", "").strip().lower() == c["price_type"].lower()), None)
    if ptype is None:
        raise OneCError(f"В 1С нет типа цен «{c['price_type']}». Есть: "
                        + ", ".join(t.get("Description", "") for t in types[:15]))
    items = _get(c, "Catalog_Номенклатура", {"$select": "Ref_Key,Description,НаименованиеПолное,Артикул",
                                             "$filter": "DeletionMark eq false and IsFolder eq false"}, session)
    currencies = {x["Ref_Key"]: x.get("Code", "") for x in _get(c, "Catalog_Валюты", {"$select": "Ref_Key,Code"},
                                                                 session)}
    prices = _get(c, f"{c['prices']}/SliceLast()",
                  {"$filter": f"{c['price_type_field']} eq {_guid(ptype['Ref_Key'])}"}, session)
    stock = {}
    if c["stock_on"]:
        params = {}
        if c["stock_account"]:
            acc = _get(c, "ChartOfAccounts_Хозрасчетный", {"$select": "Ref_Key,Code",
                                                          "$filter": f"Code eq '{c['stock_account']}'"}, session)
            if not acc:
                raise OneCError(f"В плане счетов 1С нет счёта {c['stock_account']}")
            params["$filter"] = f"Account_Key eq {_guid(acc[0]['Ref_Key'])}"
        for row in _get(c, c["stock"], params, session):
            key = row.get(c["stock_item"])
            if key and key != EMPTY:
                stock[key] = stock.get(key, 0) + float(row.get(c["stock_qty"]) or 0)
    return {"price_type": ptype, "items": items, "prices": prices, "currencies": currencies, "stock": stock,
            "stock_on": c["stock_on"]}


def build_rows(data: dict, models: set[str], current: dict[str, prajs.Price], with_vat: bool) -> tuple[list, dict]:
    """Строки прайса из данных 1С и отчёт: сопоставлено, неоднозначно, в валюте, предупреждения."""
    match = matcher(models)
    items = {x["Ref_Key"]: x for x in data["items"]}
    ptype = data["price_type"]
    note = f"1С: {ptype.get('Description', '').strip()}"
    rows, ambiguous, foreign, matched, unmatched = [], [], [], set(), 0
    for p in data["prices"]:
        item = items.get(p.get("Номенклатура_Key"))
        if item is None or p.get("Характеристика_Key", EMPTY) != EMPTY:   # помеченные на удаление, характеристики
            continue
        model, several = match(item.get("Артикул", ""), item.get("Description", ""), item.get("НаименованиеПолное", ""))
        if model is None:
            if several:
                ambiguous.append(f"{item.get('Description', '').strip()} → {', '.join(several)}")
            else:
                unmatched += 1
            continue
        code = data["currencies"].get(p.get("Валюта_Key"), RUB)
        if code and code != RUB:
            foreign.append(f"{model} ({item.get('Description', '').strip()})")
            continue
        qty, prev = data["stock"].get(item["Ref_Key"], 0), current.get(model)
        if qty > 0:
            lead = f"в наличии ({qty:g} шт.)"
        elif prev and not (data.get("stock_on") and prev.lead.startswith("в наличии")):
            lead = prev.lead                                   # срок поставки ведёт менеджер в прайсе
        else:
            lead = ""
        # цена — действующая в 1С на сегодня: дата прайса — день выгрузки, дата установки цены — в примечании
        set_on = (p.get("Period") or "")[:10]
        price = float(p.get("Цена") or 0)
        rows.append([model, f"{price:.2f}".replace(".", ","), lead, date.today().strftime("%d.%m.%Y"),
                     note + (f", цена от {date.fromisoformat(set_on).strftime('%d.%m.%Y')}" if set_on else "")])
        matched.add(model)
    warnings = []
    vat = ptype.get("ЦенаВключаетНДС")
    if vat is not None and bool(vat) != with_vat:
        warnings.append(f"Тип цен «{ptype.get('Description', '').strip()}» в 1С — цена {'включает' if vat else 'без'} "
                        f"НДС, а в настройках агента цены {'с' if with_vat else 'без'} НДС (KB_PRICES_WITH_VAT) — "
                        "итог ТКП посчитается неверно, исправьте одно из двух")
    report = {"matched": len(matched), "unmatched_items": unmatched, "ambiguous": ambiguous[:30],
              "foreign": foreign[:30], "without_price": sorted(models - matched), "warnings": warnings}
    return rows, report


def sync(session=None) -> dict:
    """Цены из 1С → прайс для ТКП. Прайс заменяется, только если хотя бы одна цена прошла проверку."""
    from .config import settings
    c = conf()
    if not configured():
        raise OneCError("1С не подключена: задайте KB_1C_URL, KB_1C_USER и KB_1C_PRICE_TYPE")
    data = fetch(c, session)
    models = prajs._known_models()
    current, _ = prajs.load()
    rows, report = build_rows(data, models, current, settings.prices_include_vat)
    if not rows:
        raise OneCError("Ни одна номенклатура 1С не сопоставлена с моделями справочника — проверьте наименования "
                        "или артикулы (например, «АКИП-4143/1»)")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(prajs.COLUMNS)
    w.writerows(rows)
    try:
        rep = prajs.replace_file("1c.csv", buf.getvalue().encode("utf-8-sig"))
    except ValueError as e:
        raise OneCError(f"Цены из 1С не прошли проверку прайса: {e}") from e
    report.update(time=time.strftime("%Y-%m-%d %H:%M:%S"), config=c["title"], price_type=c["price_type"],
                  server=re.sub(r"^https?://", "", c["url"]).split("/")[0], prices=rep.prices,
                  conflicts=[x["model"] for x in rep.conflicts], errors=len(rep.errors), stock=c["stock_on"],
                  items=len(data["items"]))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def last_report() -> dict | None:
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

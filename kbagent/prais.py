"""Прайс: цена и срок поставки модели — для таблицы ТКП (Э10-10).

Файл — CSV или XLSX с колонками «Модель; Цена; Срок поставки» (цена — в рублях за штуку). Проверка — как
у справочника параметров: пропуски, цена — положительное число, модель есть в справочнике (ТКП предлагает только
модели из него), повторы. Строки с ошибками не загружаются, при ошибках во всех строках прежний прайс остаётся.

В ТКП цена и сумма подставляются с пометкой «по прайсу от …»; у демонстрационного прайса — «ДЕМО», цены
в нём условные. Окончательно цену и срок подтверждает менеджер: ТКП всё равно утверждает сотрудник.
"""
import csv
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

DIR = settings.raw_dir.parent / "prais"
PATH = DIR / "prais.csv"
META = DIR / "prais.json"          # {"source", "date": «13.10.2026», "demo": true} — от чего и какого числа прайс
COLUMNS = ["Модель", "Цена", "Срок поставки"]
REQUIRED = ["Модель", "Цена"]


@dataclass
class Price:
    model: str
    price: float          # ₽ за штуку
    lead: str = ""        # «2 недели», «в наличии»; пусто — срок уточняет менеджер


@dataclass
class Report:
    loaded_at: str = ""
    source: str = ""
    date: str = ""
    demo: bool = False
    rows: int = 0
    prices: int = 0
    errors: list = field(default_factory=list)       # строка файла и причина — строка не загружена
    duplicates: int = 0


def _number(s: str) -> float | None:
    """«12 500», «12500,00 ₽», «12 500 руб.» → 12500.0; не число — None."""
    s = re.sub(r"(?:₽|руб\.?|р\.)\s*$", "", s.replace(" ", " ").strip(), flags=re.I).replace(" ", "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse(path: Path, known_models: set[str] | None = None) -> tuple[dict[str, Price], Report]:
    """Читает и проверяет прайс. known_models — модели справочника параметров."""
    from .spravochnik import _read_rows
    rep = Report(loaded_at=time.strftime("%Y-%m-%d %H:%M:%S"), source=path.name)
    raw = _read_rows(path)
    rep.rows = len(raw)
    missing = [c for c in REQUIRED if raw and c not in raw[0]]
    if missing:
        rep.errors.append({"line": 1, "error": f"нет колонок: {', '.join(missing)}"})
        return {}, rep
    prices: dict[str, Price] = {}
    for line, r in enumerate(raw, start=2):
        empty = [c for c in REQUIRED if not r.get(c)]
        if empty:
            rep.errors.append({"line": line, "error": f"не заполнено: {', '.join(empty)}"})
            continue
        model, value = r["Модель"], _number(r["Цена"])
        if value is None or value <= 0:
            rep.errors.append({"line": line, "error": f"цена «{r['Цена']}» — не положительное число"})
            continue
        if known_models is not None and model not in known_models:
            rep.errors.append({"line": line, "error": f"модели «{model}» нет в справочнике параметров"})
            continue
        item = Price(model, value, r.get("Срок поставки", ""))
        if model in prices:
            if prices[model].price == value:
                rep.duplicates += 1
            else:
                rep.errors.append({"line": line, "error": f"«{model}» уже есть в прайсе с другой ценой — оставлена "
                                                          "первая"})
            continue
        prices[model] = item
    rep.prices = len(prices)
    return prices, rep


def _meta() -> dict:
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_CACHE: dict = {}


def load() -> tuple[dict[str, Price], Report]:
    """Прайс с проверкой; перечитывается, только когда изменился файл прайса или справочник."""
    from . import spravochnik
    if not PATH.exists():
        return {}, Report(errors=[{"line": 0, "error": "прайс не загружен"}])
    spr_values, _ = spravochnik.load()
    stamp = (str(PATH), PATH.stat().st_mtime, META.stat().st_mtime if META.exists() else 0,
             spravochnik._CACHE.get("stamp"))
    if _CACHE.get("stamp") != stamp:
        prices, rep = parse(PATH, {v.model for v in spr_values} or None)
        meta = _meta()
        rep.source, rep.date, rep.demo = meta.get("source", rep.source), meta.get("date", ""), bool(meta.get("demo"))
        _CACHE.update(stamp=stamp, prices=prices, report=rep)
    return _CACHE["prices"], _CACHE["report"]


def replace_file(filename: str, data: bytes) -> Report:
    """Новый прайс от администратора: проверяется до замены; дата прайса — день загрузки, пометки «ДЕМО» нет."""
    from . import spravochnik
    ext = Path(filename).suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise ValueError("поддерживаются CSV и XLSX")
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"upload{ext}"
    tmp.write_bytes(data)
    try:
        prices, rep = parse(tmp, {v.model for v in spravochnik.load()[0]} or None)
    finally:
        tmp.unlink()
    if not prices:
        raise ValueError("ни одна строка не прошла проверку: " + "; ".join(e["error"] for e in rep.errors[:3]))
    if PATH.exists():
        PATH.replace(DIR / "prais.prev.csv")
    with open(PATH, "w", encoding="utf-8-sig", newline="") as f:      # хранится в одном формате — CSV
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        for p in prices.values():
            w.writerow([p.model, f"{p.price:g}", p.lead])
    META.write_text(json.dumps({"source": filename, "date": time.strftime("%d.%m.%Y"), "demo": False},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    _CACHE.clear()
    return load()[1]


def label(rep: Report) -> str:
    """«по прайсу от 13.10.2026 (ДЕМО — цены условные)»."""
    when = f" от {rep.date}" if rep.date else ""
    return f"по прайсу{when}" + (" (ДЕМО — цены условные)" if rep.demo else "")


def money(x: float) -> str:
    """12500 → «12 500 ₽»; копейки — только если есть."""
    whole = f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"
    return whole.replace(",", " ").replace(".", ",") + " ₽"

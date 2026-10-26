"""Прайс продукции для черновика ТКП: цена и срок поставки — только чтение, с проверкой при загрузке.

Файл — CSV (разделитель «;») или XLSX с колонками: Модель, Цена, Срок поставки, Действует с, Примечание.
Цена — за единицу, в рублях: «41 500», «41500,00», «41 500 ₽» читаются одинаково. Срок — текстом
(«10 рабочих дней», «в наличии»). «Действует с» — дата прайса (ДД.ММ.ГГГГ); если колонки нет, при загрузке
ставится дата загрузки. Модель называется так же, как в справочнике параметров; модель без пары в справочнике
загружается с предупреждением. Две разные цены одной модели — ошибка: цена этой модели не подставляется,
пока прайс не исправят. Повторяющиеся строки не дублируются.

Агент цены не придумывает и не считает скидки: подставляет цену из прайса и сумму «цена × количество» с пометкой
«по прайсу от …», устаревший прайс отмечает; утверждает ТКП менеджер.
"""
import csv
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import spravochnik as spr

PATH = spr.DIR / "prajs.csv"
COLUMNS = ["Модель", "Цена", "Срок поставки", "Действует с", "Примечание"]
REQUIRED = ["Модель", "Цена"]
STALE_DAYS = 90        # прайс старше — цену в ТКП нужно перепроверить


@dataclass
class Price:
    model: str
    price: float          # ₽ за единицу
    lead: str             # срок поставки
    since: str            # дата прайса, ГГГГ-ММ-ДД
    note: str = ""        # «ДЕМО: цены вымышленные»

    @property
    def stale(self) -> bool:
        return (date.today() - date.fromisoformat(self.since)).days > STALE_DAYS

    @property
    def demo(self) -> bool:
        return "ДЕМО" in self.note.upper()


@dataclass
class Report:
    loaded_at: str = ""
    source: str = ""
    rows: int = 0
    prices: int = 0
    errors: list = field(default_factory=list)       # строка файла и причина — строка не загружена
    duplicates: int = 0
    unknown: list = field(default_factory=list)      # моделей нет в справочнике параметров — загружены
    conflicts: list = field(default_factory=list)    # {model, prices} — цена модели не подставляется


def money(x: float) -> str:
    """41 500 ₽ или 1 234,50 ₽ — с неразрывным пробелом между разрядами."""
    whole = round(x, 2) == round(x)
    s = f"{x:,.0f}" if whole else f"{x:,.2f}"
    return s.replace(",", " ").replace(".", ",") + " ₽"


def _price(s: str) -> float | None:
    s = re.sub(r"(?:₽|руб\.?|р\.)$", "", re.sub(r"[\s ]", "", s.strip()), flags=re.I)
    if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", s):
        return None
    return float(s.replace(",", "."))


def _date(s: str) -> str | None:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def shown_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def parse(path: Path, known_models: set[str] | None = None, default_since: str | None = None) -> tuple[list[Price], Report]:
    """Читает и проверяет прайс. Ошибочные строки не загружаются, повторы не дублируются."""
    rep = Report(loaded_at=time.strftime("%Y-%m-%d %H:%M:%S"), source=path.name)
    raw = spr._read_rows(path)
    rep.rows = len(raw)
    missing = [c for c in REQUIRED if raw and c not in raw[0]]
    if missing:
        rep.errors.append({"line": 1, "error": f"нет колонок: {', '.join(missing)}"})
        return [], rep
    names = {spr._norm(m): m for m in known_models or ()}
    found: dict[str, list[tuple[int, Price]]] = {}
    for line, r in enumerate(raw, start=2):
        empty = [c for c in REQUIRED if not r.get(c)]
        if empty:
            rep.errors.append({"line": line, "error": f"не заполнено: {', '.join(empty)}"})
            continue
        price = _price(r["Цена"])
        if price is None or price <= 0:
            rep.errors.append({"line": line, "error": f"цена «{r['Цена']}» — не число больше нуля"})
            continue
        since = _date(r["Действует с"]) if r.get("Действует с") else default_since or date.today().isoformat()
        if since is None:
            rep.errors.append({"line": line, "error": f"дата «{r['Действует с']}» — не ДД.ММ.ГГГГ"})
            continue
        model = names.get(spr._norm(r["Модель"]), r["Модель"].strip())   # имя — как в справочнике
        if known_models is not None and model not in known_models and model not in rep.unknown:
            rep.unknown.append(model)
        item = Price(model, price, r.get("Срок поставки", ""), since, r.get("Примечание", ""))
        same = found.setdefault(model, [])
        if any((p.price, p.lead, p.since) == (item.price, item.lead, item.since) for _, p in same):
            rep.duplicates += 1
            continue
        same.append((line, item))
    prices = []
    for model, items in found.items():
        if len({p.price for _, p in items}) > 1:
            rep.conflicts.append({"model": model, "prices": [f"{money(p.price)} (строка {n})" for n, p in items]})
            continue
        prices.append(items[0][1])
    rep.prices = len(prices)
    return prices, rep


_CACHE: dict = {}


def _known_models() -> set[str]:
    values, _ = spr.load()
    return {v.model for v in values}


def load(path: Path | None = None) -> tuple[dict[str, Price], Report]:
    """Прайс по моделям; перечитывается, только когда изменился файл прайса или справочника."""
    path = path or PATH
    if not path.exists():
        return {}, Report(errors=[{"line": 0, "error": "прайс не загружен"}])
    stamp = (str(path), path.stat().st_mtime, spr.PATH.stat().st_mtime if spr.PATH.exists() else 0)
    if _CACHE.get("stamp") != stamp:
        prices, rep = parse(path, _known_models())
        _CACHE.update(stamp=stamp, prices={p.model: p for p in prices}, report=rep)
    return _CACHE["prices"], _CACHE["report"]


def replace_file(filename: str, data: bytes) -> Report:
    """Новый прайс от администратора: проверяется до замены; если ни одна строка не прошла — не ставится."""
    ext = Path(filename).suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise ValueError("поддерживаются CSV и XLSX")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_name(f"prajs-upload{ext}")
    tmp.write_bytes(data)
    try:
        prices, rep = parse(tmp, _known_models(), default_since=date.today().isoformat())
    finally:
        tmp.unlink()
    if not prices:
        problems = rep.errors[:3] + [{"error": f"разные цены у {c['model']}"} for c in rep.conflicts[:3]]
        raise ValueError("ни одна строка не прошла проверку: " + "; ".join(e["error"] for e in problems[:3]))
    if PATH.exists():
        PATH.replace(PATH.with_name("prajs.prev.csv"))
    with open(PATH, "w", encoding="utf-8-sig", newline="") as f:     # хранится в одном формате — CSV
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        for p in prices:
            amount = f"{p.price:.0f}" if round(p.price, 2) == round(p.price) else f"{p.price:.2f}".replace(".", ",")
            w.writerow([p.model, amount, p.lead, shown_date(p.since), p.note])
    _CACHE.clear()
    rep.prices = len(prices)
    return rep

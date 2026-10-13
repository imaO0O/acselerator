"""Расчёты по согласованным правилам — программой, а не языковой моделью.

«Пришла партия из 250 резисторов: сколько осмотреть и сколько проверить?» — объём выборки считается
по правилам из data/pravila_raschetov.yaml (процент, минимум, сплошной контроль, округление), каждое
правило ссылается на пункт документа. Модель такие числа не вычисляет: «12–13 шт.» вместо однозначного
ответа — ровно то, что запрещает требование заказчика.
"""
import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import settings

PATH = settings.raw_dir.parent / "pravila_raschetov.yaml"
QTY_RE = re.compile(r"(?:парти\w+\s+(?:из\s+)?|в\s+партии\s+)(\d[\d\s]*\d|\d+)|(\d[\d\s]*\d|\d+)\s*(?:шт\.?|штук\w*|изделий|"
                    r"резистор\w*|конденсатор\w*|микросхем\w*|ЭРИ|компонент\w*|единиц\w*)", re.I)
INTENT_RE = re.compile(r"сколько\b[^?]*\b(?:осмотреть|проверить|отобрать|проконтролировать|взять|контролировать)|"
                       r"объ[её]м\w*\s+выборк\w*|выборк\w*", re.I)


@dataclass
class Rule:
    id: str
    title: str
    doc: str
    clause: str
    when: str
    percent: float
    minimum: int
    rounding: str = "вверх"
    full_up_to: int | None = None
    agreed: bool = False


@dataclass
class Result:
    rule: Rule
    quantity: int
    value: int
    explanation: str


def load_rules(path: Path | None = None) -> list[Rule]:
    path = path or PATH
    if not path.exists():
        return []
    return [Rule(**r) for r in yaml.safe_load(path.read_text(encoding="utf-8")).get("rules", [])]


def compute(rule: Rule, n: int) -> Result:
    num = lambda x: f"{x:g}".replace(".", ",")  # noqa: E731
    if rule.full_up_to and n <= rule.full_up_to:
        return Result(rule, n, n, f"партия до {rule.full_up_to} шт. включительно — сплошной контроль, все {n} шт.")
    raw = n * rule.percent / 100
    rounded = math.ceil(raw) if rule.rounding == "вверх" else math.floor(raw) if rule.rounding == "вниз" else round(raw)
    value = min(n, max(rounded, rule.minimum))
    parts = [f"{num(rule.percent)} % от {n} = {num(raw)}"]
    if rounded != raw:
        parts.append(f"округление {rule.rounding} — {rounded}")
    if rounded < rule.minimum:
        parts.append(f"но не менее {rule.minimum} — {value}")
    else:
        parts.append(f"не менее {rule.minimum}")
    return Result(rule, n, value, "; ".join(parts))


def match(question: str, rules: list[Rule] | None = None) -> list[Result]:
    """Расчёты для вопроса: есть количество и вопрос «сколько осмотреть / проверить / какой объём выборки».
    Подходят правила, чьи слова есть в вопросе; если вопрос общий («какой объём выборки») — все правила."""
    rules = load_rules() if rules is None else rules
    qty = QTY_RE.search(question)
    if not rules or not qty or not INTENT_RE.search(question):
        return []
    n = int(re.sub(r"\s", "", qty.group(1) or qty.group(2)))
    chosen = [r for r in rules if re.search(r.when, question, re.I)] or rules
    return [compute(r, n) for r in chosen]

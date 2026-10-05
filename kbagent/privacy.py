"""Персональные данные в журналах (152-ФЗ): маскирование и срок хранения.

Перед записью в журнал вопросов и в оценки маскируются телефоны, почта, паспорт, СНИЛС, ИНН человека
и полные ФИО с отчеством — то, что сотрудник может вписать в вопрос. Рабочие сведения остаются:
«ответственный — Белов И. Н.» (инициалы), внутренний телефон 2211 — без них журнал бесполезен для разбора.
Журнал вопросов хранится settings.log_retention_days дней; журнал действий (входы, загрузки) — для службы
безопасности, его срок задаёт она сама.
"""
import json
import re
import time

from .config import settings

PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[почта]"),
    (re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "[телефон]"),
    (re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)"), "[СНИЛС]"),
    (re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)"), "[паспорт]"),
    (re.compile(r"(?<![\d-])\d{12}(?![\d-])"), "[ИНН]"),
    # Фамилия Имя Отчество полностью (отчество на -вич, -вна, -ична, -чна)
    (re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна)\b"), "[ФИО]"),
]


def mask(text: str | None) -> str | None:
    if not text or not settings.log_mask_pii:
        return text
    for rx, label in PATTERNS:
        text = rx.sub(label, text)
    return text


def prune(path, days: int | None = None) -> int:
    """Удаляет из журнала (jsonl с полем time «ГГГГ-ММ-ДД …») записи старше срока; возвращает, сколько удалено."""
    days = settings.log_retention_days if days is None else days
    if not days or not path.exists():
        return 0
    border = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    lines = path.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if ln.strip() and json.loads(ln).get("time", "9999") >= border]
    if len(keep) != len([ln for ln in lines if ln.strip()]):
        path.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    return len(lines) - len(keep)

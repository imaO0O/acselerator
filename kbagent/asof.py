"""Ответ на дату: «какой срок поверки действовал на 01.03.2025?», «как оформляли отказ в 2024 году?».

Отменённые редакции остаются в индексе (их не видит ни одна роль), в реестре у документов — даты вступления в силу и
окончания действия. Если в вопросе прошедшая дата, месяц или год, поиск идёт только по документам, которые тогда
действовали и доступны роли по своей группе: прежняя редакция — от своей даты до вступления в силу новой. К ответу
добавляется, какие редакции действовали в это время и что действует сейчас. Нужно ОТК, метрологии и при разборе
рекламаций: по какой редакции принимали партию, какой был срок поверки. Модель не участвует — только отбор документов.
"""
import datetime as dt
import re
from dataclasses import dataclass

from .ingest import _date, _ru, group_of, is_cancelled, is_draft_edition

MONTHS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
MONTH_FORMS = r"январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|июл[яе]|август[ае]?|сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе]"
MONTH_NAMES = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре",
               "ноябре", "декабре"]
DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\d.])")          # год — 4 цифры: «п. 4.2.10» — не дата
TEXT_DATE_RE = re.compile(rf"(?<!\d)(\d{{1,2}})\s+({MONTH_FORMS})\s+(\d{{4}})", re.I)
MONTH_RE = re.compile(rf"\b(?:в|на)\s+({MONTH_FORMS})\s+(\d{{4}})", re.I)
YEAR_RE = re.compile(r"\b(?:в|на|за)\s+(\d{4})\s*(?:-?м\s+)?(?:году|год\b|г\.)", re.I)
LAST_YEAR_RE = re.compile(r"\bв\s+(поза)?прошлом\s+году\b", re.I)
YEAR_AGO_RE = re.compile(r"\b(?:год|полгода)\s+назад\b", re.I)


@dataclass(frozen=True)
class Window:
    start: str          # 2025-03-01
    end: str            # включительно
    label: str          # «на 01.03.2025», «в марте 2025 года», «в 2024 году»


def _month(word: str) -> int:
    word = word.lower()
    return next(i for i, stem in enumerate(MONTHS, 1) if word.startswith(stem))


def _day(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _month_end(y: int, m: int) -> dt.date:
    return (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1))


def parse(question: str, today: dt.date | None = None) -> Window | None:
    """Прошедшая дата, месяц или год в вопросе; будущее и сегодняшний день — не «на дату» (действует текущее)."""
    today = today or dt.date.today()
    window = None
    if m := DATE_RE.search(question) or TEXT_DATE_RE.search(question):
        month = int(m.group(2)) if m.re is DATE_RE else _month(m.group(2))
        if day := _day(int(m.group(3)), month, int(m.group(1))):
            window = (day, day, f"на {day:%d.%m.%Y}")
    elif m := MONTH_RE.search(question):
        y, month = int(m.group(2)), _month(m.group(1))
        window = (dt.date(y, month, 1), _month_end(y, month), f"в {MONTH_NAMES[month - 1]} {y} года")
    elif m := YEAR_RE.search(question):
        y = int(m.group(1))
        window = (dt.date(y, 1, 1), dt.date(y, 12, 31), f"в {y} году")
    elif m := LAST_YEAR_RE.search(question):
        y = today.year - (2 if m.group(1) else 1)
        window = (dt.date(y, 1, 1), dt.date(y, 12, 31), f"в {y} году")
    elif m := YEAR_AGO_RE.search(question):
        day = today - dt.timedelta(days=182 if m.group(0).lower().startswith("пол") else 365)
        window = (day, day, f"на {day:%d.%m.%Y}")
    if window is None or window[0] >= today or window[0].year < 1990:
        return None
    start, end, label = window
    return Window(start.isoformat(), min(end, today).isoformat(), label)


def _day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()


def period(doc_id: str, catalog: dict) -> tuple[str, str] | None:
    """Когда документ действовал: (с, по), пусто — без ограничения; None — неизвестно, когда его отменили."""
    info = catalog.get(doc_id, {})
    start, end = _date(info.get("effective_from")), _date(info.get("effective_to"))
    new = catalog.get(info.get("replaced_by") or "")
    if new is not None:
        since = _date(new.get("effective_from")) or str(new.get("approved_at") or new.get("uploaded_at") or "")[:10]
        if since:
            end = min(end, _day_before(since)) if end else _day_before(since)
    if is_cancelled(info) and not end:
        return None             # отменён, а с какого дня — неизвестно: не утверждаем, что он тогда действовал
    return start, end


def current_edition(doc_id: str, catalog: dict) -> str:
    """Действующая сейчас редакция: по цепочке «заменён на»."""
    seen = {doc_id}
    while (nxt := catalog.get(doc_id, {}).get("replaced_by")) and nxt not in seen:
        seen.add(nxt)
        doc_id = nxt
    return doc_id


def scope(window: Window, allowed: set[str], doc_ids: set[str], catalog: dict) -> set[str]:
    """Документы, действовавшие в это время и доступные роли по своей группе (не по пометке «отменён»)."""
    out = set()
    for doc_id in doc_ids:
        info = catalog.get(doc_id)
        if info is None:                    # нет в реестре — сроков не знаем, считаем действующим
            out.add(doc_id)
            continue
        if is_draft_edition(info) or group_of(info) not in allowed:
            continue
        when = period(doc_id, catalog)
        if when and (not when[0] or when[0] <= window.end) and (not when[1] or when[1] >= window.start):
            out.add(doc_id)
    return out


def _overlapping(current: str, window: Window, catalog: dict) -> set[str]:
    """Все редакции одного документа (по цепочке «заменён на»), действовавшие в это время."""
    out = set()
    for doc_id in catalog:
        if current_edition(doc_id, catalog) == current and (when := period(doc_id, catalog)) is not None \
                and (not when[0] or when[0] <= window.end) and (not when[1] or when[1] >= window.start):
            out.add(doc_id)
    return out


def _span(when: tuple[str, str] | None) -> str:
    if not when or not any(when):
        return ""
    start, end = when
    return (f"с {_ru(start)}" if start else "") + (f"{' ' if start else ''}по {_ru(end)}" if end else
                                                   (" — действует и сейчас" if start else ""))


def note(window: Window, cited: list[str], catalog: dict) -> str:
    """«На 01.06.2025 действовал «СТП ДП-02-2024…» — с 15.01.2024 по 28.02.2026; сейчас действует «…»»."""
    cited = list(dict.fromkeys(cited))
    if not cited:
        return ""
    parts, switched = [], False
    for doc_id in cited:
        title = catalog.get(doc_id, {}).get("title", doc_id)
        line = f"«{title}»" + (f" ({span})" if (span := _span(period(doc_id, catalog))) else "")
        now = current_edition(doc_id, catalog)
        # редакция сменилась внутри периода («в 2026 году»): называем и другую — ответ мог быть по любой
        for other in sorted(_overlapping(now, window, catalog) - {doc_id}):
            other_span = _span(period(other, catalog))
            line += f"; в этот же период действовал и «{catalog[other].get('title', other)}»" + (
                f" ({other_span})" if other_span else "")
            switched = True
        if now != doc_id:
            line += f"; сейчас действует «{catalog.get(now, {}).get('title', now)}»"
        parts.append(line)
    head = window.label[:1].upper() + window.label[1:]
    if not switched and all(current_edition(d, catalog) == d for d in cited):
        return f"**{head}** действовали те же редакции, что и сейчас: " + "; ".join(parts) + "."
    return f"**{head}** действовал{'и' if len(parts) > 1 else ''}: " + "; ".join(parts) + "."

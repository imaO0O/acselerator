"""Вопросы эксперту: противоречия между источниками и вопросы, которые сотрудник передал вручную.

Агент не выбирает между расходящимися значениями сам — он показывает оба и ставит вопрос в очередь
эксперту (владельцу документа или справочника). Эксперт видит очередь на странице «Администрирование»,
отмечает решение; открытый вопрос по той же теме повторно не заводится. Если эксперт выбрал верное значение,
агент дальше отвечает им — с пометкой «подтверждено экспертом» — и вопрос больше не поднимает.
"""
import json
import time
import uuid

from .config import settings


PATH = None          # другой файл очереди (эталонный прогон не должен видеть решений с показа)


def _path():
    return PATH or settings.log_dir / "experts.json"


def load() -> list[dict]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def add(kind: str, subject: str, details: list | str, question: str = "", user: str | None = None,
        role: str | None = None, comment: str = "") -> dict:
    """Новый вопрос эксперту; если по этой теме уже есть открытый — к нему добавляется ещё одно обращение."""
    items = load()
    for it in items:
        if it["subject"] == subject and it["status"] == "открыт":
            it["asked"] = it.get("asked", 1) + 1
            it["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if comment:
                it["comment"] = "; ".join(x for x in (it.get("comment"), comment) if x)
            _save(items)
            return it
    it = {"id": uuid.uuid4().hex[:8], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "subject": subject,
          "details": details, "question": question, "user": user, "role": role, "status": "открыт", "asked": 1}
    if comment:
        it["comment"] = comment
    items.append(it)
    _save(items)
    return it


def get(item_id: str) -> dict | None:
    return next((it for it in load() if it["id"] == item_id), None)


def resolve(item_id: str, user: str | None, note: str = "", chosen: dict | None = None) -> None:
    """chosen — верное значение из расходящихся ({value, doc, page}); без него — решение текстом."""
    items = load()
    for it in items:
        if it["id"] == item_id:
            it.update(status="решён", resolved_by=user, resolved_at=time.strftime("%Y-%m-%d %H:%M:%S"), note=note)
            if chosen:
                it["chosen"] = chosen
    _save(items)


def decisions(kind: str = "противоречие в справочнике") -> dict[str, dict]:
    """Решённые вопросы по теме: тема («АКИП-4115/1В — Частота дискретизации на канал») → последнее решение."""
    out = {}
    for it in sorted(load(), key=lambda x: x.get("resolved_at", "")):
        if it.get("kind") == kind and it["status"] == "решён":
            out[it["subject"]] = it
    return out

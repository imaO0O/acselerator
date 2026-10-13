"""Вопросы эксперту: противоречия между источниками и вопросы, которые сотрудник передал вручную.

Агент не выбирает между расходящимися значениями сам — он показывает оба и ставит вопрос в очередь
эксперту (владельцу документа или справочника). Эксперт видит очередь на странице «Администрирование»,
отмечает решение; открытый вопрос по той же теме повторно не заводится.
"""
import json
import time
import uuid

from .config import settings


def _path():
    return settings.log_dir / "experts.json"


def load() -> list[dict]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def add(kind: str, subject: str, details: list | str, question: str = "", user: str | None = None,
        role: str | None = None) -> dict:
    """Новый вопрос эксперту; если по этой теме уже есть открытый — к нему добавляется ещё одно обращение."""
    items = load()
    for it in items:
        if it["subject"] == subject and it["status"] == "открыт":
            it["asked"] = it.get("asked", 1) + 1
            it["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(items)
            return it
    it = {"id": uuid.uuid4().hex[:8], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "subject": subject,
          "details": details, "question": question, "user": user, "role": role, "status": "открыт", "asked": 1}
    items.append(it)
    _save(items)
    return it


def resolve(item_id: str, user: str | None, note: str = "") -> None:
    items = load()
    for it in items:
        if it["id"] == item_id:
            it.update(status="решён", resolved_by=user, resolved_at=time.strftime("%Y-%m-%d %H:%M:%S"), note=note)
    _save(items)

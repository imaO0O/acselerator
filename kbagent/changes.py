"""Уведомления об изменениях документов: сотрудник узнаёт о новой редакции, не спрашивая.

Событие пишется, когда утверждена новая редакция, загружен, обновлён или удалён документ, а также когда папка
отдела подключила, обновила или убрала файл (logs/changes.jsonl, только дописывается). Вступление в силу и окончание
срока действия берутся из дат реестра — событие появляется в свой день. Сотрудник видит в чате только изменения
документов, доступных его роли, с итогом сравнения («изменено пунктов: 3, добавлено: 1») и кнопкой «Что изменилось»;
ответы в его диалогах, опиравшиеся на изменившийся документ, помечаются — их стоит переспросить. Что сотрудник уже
видел — logs/changes_seen.json. Модель не участвует: сравнение — по пунктам (kbagent/editions.py).
"""
import datetime as dt
import hashlib
import json
import time

from .config import settings

KINDS = {"edition": "Новая редакция", "added": "Новый документ", "updated": "Документ обновлён",
         "removed": "Документ убран из базы", "effective": "Вступил в силу", "expired": "Истёк срок действия"}
FIRST_DAYS = 14         # новому пользователю — изменения за последние две недели
MAX_CHANGES = 30        # сколько изменённых пунктов хранить в событии (итог — по всем)
_CACHE: dict = {}


def _path():
    return settings.log_dir / "changes.jsonl"


def _seen_path():
    return settings.log_dir / "changes_seen.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def summary(cmp) -> str:
    """«изменено пунктов: 3, добавлено: 1, исключено: 1» по итогу сравнения (editions.Comparison)."""
    parts = [f"{word}: {cmp.count(kind)}" for kind, word in (("changed", "изменено"), ("added", "добавлено"),
                                                              ("removed", "исключено")) if cmp.count(kind)]
    return "пунктов " + ", ".join(parts) if parts else "текст пунктов не изменился"


def _changes(cmp) -> list[dict]:
    out = []
    for c in cmp.changes[:MAX_CHANGES]:
        clause = c.new or c.old
        out.append({"kind": c.kind, "number": clause.number, "page": clause.page,
                    "text": c.diff if c.kind == "changed" else clause.text[:500]})
    return out


def record(kind: str, doc_id: str, title: str, access: str = "all", by: str | None = None,
           old_id: str | None = None, old_title: str | None = None, cmp=None, note: str = "",
           ack: bool | None = None) -> dict:
    """ack — нужно ли ознакомление под отметку; по умолчанию — для новой редакции (ACK_KINDS)."""
    event = {"id": hashlib.sha1(f"{_now()}{kind}{doc_id}".encode()).hexdigest()[:10], "time": _now(), "kind": kind,
             "doc_id": doc_id, "title": title, "access": access or "all", "by": by or "—"}
    if ack is not None:
        event["ack"] = ack
    if old_id:
        event.update(old_id=old_id, old_title=old_title or old_id)
    if cmp is not None:
        event.update(summary=summary(cmp), changes=_changes(cmp))
    elif note:
        event["summary"] = note
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def load() -> list[dict]:
    path = _path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    if _CACHE.get("stamp") != (str(path), stamp):
        lines = path.read_text(encoding="utf-8").splitlines()
        _CACHE.update(stamp=(str(path), stamp), events=[json.loads(x) for x in lines if x.strip()])
    return _CACHE["events"]


def dated(catalog: dict, since: str, now: str | None = None) -> list[dict]:
    """События по датам реестра после since: вступление в силу (утверждённого заранее) и окончание срока."""
    from .ingest import _date, _ru, group_of, is_draft_edition
    now = now or _now()
    today = now[:10]
    previous = {info["replaced_by"]: doc_id for doc_id, info in catalog.items() if info.get("replaced_by")}
    out = []
    for doc_id, info in catalog.items():
        if is_draft_edition(info):
            continue
        start, end = _date(info.get("effective_from")), _date(info.get("effective_to"))
        made = str(info.get("approved_at") or info.get("uploaded_at") or "")[:10]
        base = {"doc_id": doc_id, "title": info.get("title", doc_id), "access": group_of(info), "by": "реестр"}
        # утверждён в тот же день или позже даты вступления — событие уже записано при утверждении
        if start and since < f"{start} 00:00:00" and start <= today and (not made or made < start):
            old = previous.get(doc_id) or info.get("replaces")
            # ознакомление — только для документа, внесённого через систему (загрузка, утверждение): реестр,
            # заведённый при установке, не требует отметок задним числом
            event = {**base, "id": f"from-{doc_id}-{start}", "time": f"{start} 00:00:00",
                     "kind": "edition" if old else "effective", "summary": f"действует с {_ru(start)}",
                     "ack": bool(made)}
            if old:
                event.update(old_id=old, old_title=catalog.get(old, {}).get("title", old))
            out.append(event)
        if end:
            after = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
            if since < f"{after} 00:00:00" and after <= today:
                out.append({**base, "id": f"to-{doc_id}-{end}", "time": f"{after} 00:00:00", "kind": "expired",
                            "summary": f"действовал по {_ru(end)}"})
    return out


def seen_at(login: str) -> str | None:
    try:
        return json.loads(_seen_path().read_text(encoding="utf-8")).get(login)
    except (OSError, ValueError):
        return None


def mark_seen(login: str, when: str | None = None) -> None:
    path = _seen_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[login] = when or _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def feed(login: str, role: str, catalog: dict | None = None, now: str | None = None,
         since: str | None = None, clearance: int | None = None) -> list[dict]:
    """Ещё не просмотренные изменения документов, доступных роли, — новые сверху; свои загрузки не показываются.
    since — с этого момента, а не с прошлого просмотра (REST API)."""
    from . import users
    from .ingest import allowed_groups, registry
    now = now or _now()
    since = since or seen_at(login) or (dt.datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                                        - dt.timedelta(days=FIRST_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    allowed = allowed_groups(role, users.clearance(login) if clearance is None else clearance)
    catalog = registry() if catalog is None else catalog
    events = [e for e in load() if since < e["time"] <= now] + dated(catalog, since, now)
    # с чем ещё нужно ознакомиться — показывается и после «Понятно», пока сотрудник не отметит «Ознакомлен»
    events += [e for e in ack_events(catalog, now) if not acked_at(login, e["id"])]
    latest: dict = {}
    for e in sorted(events, key=lambda e: e["time"]):
        if e.get("access", "all") in allowed and e.get("by") != login:
            latest[(e["kind"], e["doc_id"])] = e          # повтор того же — остаётся последнее
    return sorted(({**e, "pending_ack": needs_ack(e) and not acked_at(login, e["id"])} for e in latest.values()),
                  key=lambda e: e["time"], reverse=True)


# ---------- лист ознакомления ----------
# Новая редакция и вступление в силу — сотрудник отмечает «Ознакомлен» (так требует СМК: ISO 9001, лист
# ознакомления); для обновлённого администратором документа — тоже, для файлов папки отдела — нет (их правят часто).
ACK_KINDS = {"edition", "effective"}
ACK_DAYS = 365          # сколько ждать отметки: старше — из напоминаний в чате уходит, в листе остаётся


def _acks_path():
    return settings.log_dir / "acks.jsonl"


def needs_ack(event: dict) -> bool:
    return bool(event.get("ack", event["kind"] in ACK_KINDS))


def acks() -> list[dict]:
    path = _acks_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    if _CACHE.get("acks_stamp") != (str(path), stamp):
        lines = path.read_text(encoding="utf-8").splitlines()
        _CACHE.update(acks_stamp=(str(path), stamp), acks=[json.loads(x) for x in lines if x.strip()])
    return _CACHE["acks"]


def acked_at(login: str, event_id: str) -> str | None:
    return next((a["time"] for a in acks() if a["login"] == login and a["event_id"] == event_id), None)


def acknowledge(login: str, name: str, role: str, event: dict) -> None:
    from .actions import log_action
    if acked_at(login, event["id"]):
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    with open(_acks_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": _now(), "login": login, "name": name, "role": role, "event_id": event["id"],
                            "doc_id": event["doc_id"], "title": event["title"]}, ensure_ascii=False) + "\n")
    log_action("ack", login, event["title"], changes_label(event))


def changes_label(event: dict) -> str:
    return KINDS[event["kind"]] + (f" вместо «{event['old_title']}»" if event.get("old_title") else "")


def ack_events(catalog: dict | None = None, now: str | None = None, days: int = ACK_DAYS) -> list[dict]:
    """Изменения, с которыми нужно ознакомиться, за days дней — новые сверху."""
    from .ingest import registry
    now = now or _now()
    since = (dt.datetime.strptime(now, "%Y-%m-%d %H:%M:%S") - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    catalog = registry() if catalog is None else catalog
    events = [e for e in load() + dated(catalog, since, now) if since < e["time"] <= now and needs_ack(e)]
    return sorted({e["id"]: e for e in events}.values(), key=lambda e: e["time"], reverse=True)


def sheet(event: dict, people: list[dict]) -> list[dict]:
    """Лист ознакомления: кому документ доступен по роли (кроме загрузившего) и когда отмечено «Ознакомлен»."""
    from .ingest import allowed_groups
    rows = []
    for u in people:
        if u.get("active", True) and u["login"] != event.get("by") and \
                event.get("access", "all") in allowed_groups(u["role"], u.get("clearance", 0)):
            rows.append({"login": u["login"], "name": u.get("name", u["login"]), "role": u["role"],
                         "at": acked_at(u["login"], event["id"])})
    return sorted(rows, key=lambda r: (r["at"] is not None, r["name"]))


def _stamp(item: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.get("ts") or 0))


def stale(item: dict, catalog: dict | None = None) -> list[str]:
    """Почему ответ в диалоге мог устареть: после него изменился документ, на который он ссылается.

    Ответ «на дату» не устаревает — он и должен ссылаться на прежнюю редакцию."""
    from .ingest import _ru, registry
    if item.get("status") != "answer" or item.get("as_of") or not item.get("ts"):
        return []
    cited = {s["doc_id"] for s in item.get("sources", []) if s.get("cited")}
    if not cited:
        return []
    asked = _stamp(item)
    catalog = registry() if catalog is None else catalog
    reasons = []
    for e in sorted(load() + dated(catalog, asked), key=lambda e: e["time"]):
        if e["time"] <= asked:
            continue
        day = _ru(e["time"][:10])
        if e["kind"] == "edition" and e.get("old_id") in cited:
            reasons.append(f"«{e['old_title']}» заменён новой редакцией «{e['title']}» ({day})")
        elif e["doc_id"] in cited and e["kind"] == "updated":
            reasons.append(f"«{e['title']}» обновлён {day}" + (f": {e['summary']}" if e.get("summary") else ""))
        elif e["doc_id"] in cited and e["kind"] == "removed":
            reasons.append(f"«{e['title']}» убран из базы знаний {day}")
        elif e["doc_id"] in cited and e["kind"] == "expired":
            reasons.append(f"у «{e['title']}» истёк срок действия ({e['summary']})")
    return list(dict.fromkeys(reasons))


def affected_answers(conversations: list[dict], events: list[dict]) -> dict[str, int]:
    """Сколько ответов в диалогах сотрудника опирались на документ до его изменения — по каждому событию."""
    out = {}
    for e in events:
        doc = e.get("old_id") if e["kind"] == "edition" else e["doc_id"]
        n = sum(1 for conv in conversations for item in conv.get("items", [])
                if _stamp(item) < e["time"] and not item.get("as_of")
                and any(s.get("cited") and s["doc_id"] == doc for s in item.get("sources", [])))
        if n:
            out[e["id"]] = n
    return out

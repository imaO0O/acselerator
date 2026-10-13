"""Польза в цифрах — мерило партнёра: время подготовки проверенного ответа.

Каждое утверждённое сотрудником (черновик ответа или ТКП) записывается: сколько прошло от вопроса до
утверждения, какие источники использованы. Отсюда — медиана времени против ручной подготовки и сэкономленные
часы. Ручное время — оценка до замера (`docs/pilot/zamer_i_stoimost.md`): 15 минут инженера на ответ;
на ТКП — 60 минут. После замера на запросах партнёра оценка заменяется измеренным временем.

Здесь же — ответы клиентам, которые нужно перепроверить: после утверждения источник изменился (документ
заменён новой редакцией, истёк срок действия, эксперт исправил значение в справочнике).
"""
import json
import statistics
import time
import uuid

from .config import settings

BASELINE_MIN = {"ответ": 15, "ТКП": 60}     # ручная подготовка, мин — оценка до замера
HOUR_COST = 630                              # ₽ в час инженера (docs/pilot/zamer_i_stoimost.md)
PATH = None


def _path():
    return PATH or settings.log_dir / "effect.jsonl"


def record(kind: str, user: str | None, role: str | None, question: str, seconds: float,
           sources: list[dict], positions: int = 0) -> str:
    """Утверждённый результат: kind — «ответ» или «ТКП»; sources — [{doc_id, page}] процитированных источников."""
    rec = {"id": uuid.uuid4().hex[:8], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "user": user,
           "role": role, "question": question[:300], "seconds": round(max(seconds, 0.0), 1),
           "sources": [{"doc_id": s.get("doc_id"), "page": s.get("page")} for s in sources if s.get("doc_id")],
           "positions": positions}
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["id"]


def load() -> list[dict]:
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summary(records: list[dict] | None = None) -> dict:
    """По видам результата: сколько утверждено, медиана минут до утверждения, ручная оценка, сэкономлено."""
    records = load() if records is None else records
    out = {"kinds": {}, "saved_hours": 0.0, "saved_rub": 0}
    for kind, base in BASELINE_MIN.items():
        recs = [r for r in records if r.get("kind") == kind]
        if not recs:
            continue
        minutes = [r["seconds"] / 60 for r in recs]
        median = statistics.median(minutes)
        saved = sum(max(base - m, 0) for m in minutes) / 60
        out["kinds"][kind] = {"count": len(recs), "median_min": median, "baseline_min": base,
                              "share": median / base, "saved_hours": saved}
        out["saved_hours"] += saved
    out["saved_rub"] = round(out["saved_hours"] * HOUR_COST)
    return out


def affected(records: list[dict] | None = None) -> list[dict]:
    """Утверждённые ответы и ТКП, чьи источники изменились после утверждения."""
    from .experts import load as load_experts
    from .ingest import ARCHIVE, access_of, expired_on, registry_info
    records = load() if records is None else records
    fixes = [it for it in load_experts() if it.get("status") == "решён" and isinstance(it.get("details"), list)]
    out = []
    for r in records:
        reasons = []
        for s in r.get("sources", []):
            info = registry_info(s["doc_id"])
            title = info.get("title", s["doc_id"])
            if info and access_of(info) == ARCHIVE:
                reasons.append(f"«{title}» заменён новой редакцией")
            elif expired_on(info):
                reasons.append(f"у «{title}» истёк срок действия")
            for it in fixes:
                if it.get("resolved_at", "") >= r["time"] and any(
                        d.get("doc") == s["doc_id"] and d.get("page") == s.get("page") for d in it["details"]):
                    reasons.append(f"эксперт исправил значение: {it['subject']} — {it.get('note') or 'решено'}")
        if reasons:
            out.append({**r, "reasons": list(dict.fromkeys(reasons))})
    return out

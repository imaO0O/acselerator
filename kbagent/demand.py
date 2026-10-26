"""Аналитика спроса: что спрашивают клиенты и чего не хватает в справочнике и ассортименте.

Каждый разбор письма клиента и каждый подбор или проверка по справочнику записываются в logs/demand.jsonl — без
персональных данных: вид продукции, модель, количество, требования и итог по позиции (предложено, ни одна модель
не подходит, нет данных, нет в справочнике). Отсюда для руководителя:
  - какие виды продукции и модели чаще всего запрашивают и предлагают;
  - какие запросы не закрыты и почему — что дописать в справочник или добавить в ассортимент;
  - какие требования клиентов встречаются чаще всего;
  - из реестра ТКП — выигрыши, проигрыши и причины проигрышей (комментарий при смене статуса на «проигран»).
Прогоны эталона и проверки перед показом (пользователь eval) в аналитику не попадают.
"""
import collections
import datetime as dt
import json
import time

from .config import settings

PATH = None
SERVICE_USERS = {None, "", "eval"}
OFFERED = ("offer", "named")
UNMET = {"not_in_catalog": "нет в справочнике", "no_fit": "ни одна модель не подходит",
         "expert": "нет данных или источники расходятся", "no_reqs": "клиент не указал требования"}


def _path():
    return PATH or settings.log_dir / "demand.jsonl"


def record(source: str, positions: list[dict], role: str | None = None, user: str | None = None) -> None:
    """source — «письмо клиента» или «подбор»; positions — [{kind, model, status, qty, reqs, unmet}]."""
    if user in SERVICE_USERS or not positions:
        return
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source, "role": role, "user": user,
           "positions": positions}
    _path().parent.mkdir(parents=True, exist_ok=True)
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def from_tkp(t: dict) -> list[dict]:
    """Позиции разбора письма клиента — в запись спроса."""
    out = []
    for p in t.get("positions", []):
        status = p.get("status") or ""
        unmet = "; ".join(f"{c['param'].lower()} {c['need']}".strip() for c in p.get("checks", [])
                          if c.get("ok") is False)
        out.append({"kind": p.get("kind") or "", "model": (p.get("model") or "") if status in OFFERED else "",
                    "status": status, "qty": int(p.get("qty") or 1), "reqs": list(p.get("reqs") or []),
                    "unmet": unmet})
    return out


def from_selection(d: dict | None) -> list[dict]:
    """Подбор («осциллограф от 150 МГц и 4 канала») или проверка названной модели («подойдёт ли MAS830L для 15 А»)
    по справочнику. Просто значение параметра («полоса АКИП-4115/2В») — не запрос на подбор, не записывается."""
    if not d or not d.get("reqs"):
        return []
    base = {"kind": d.get("kind") or "", "qty": 1, "reqs": list(d["reqs"])}
    unmet = "; ".join(d["reqs"])
    if d.get("named"):
        out = []
        for m in d["named"]:
            status = "offer" if m in d.get("fit", []) else "no_fit" if m in d.get("unfit", []) else "expert"
            out.append({**base, "model": m if status == "offer" else "", "status": status,
                        "unmet": "" if status == "offer" else unmet, "asked_model": m})
        return out
    if d.get("fit"):
        return [{**base, "model": "", "status": "offer", "unmet": ""}]
    status = "expert" if d.get("unknown") and not d.get("unfit") else "no_fit"
    return [{**base, "model": "", "status": status, "unmet": unmet}]


def load(since: dt.date | None = None) -> list[dict]:
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("user") in SERVICE_USERS:
            continue
        if since and rec.get("time", "")[:10] < since.isoformat():
            continue
        out.append(rec)
    return out


def _lost_reason(r: dict) -> str:
    """«цена; у конкурента дешевле на 12 %» → «цена»: типовая причина складывается, подробности — в истории ТКП."""
    for h in reversed(r.get("history", [])):
        if h.get("status") == "проигран":
            return (h.get("comment") or "").split(";")[0].strip() or "причина не указана"
    return "причина не указана"


def summary(records: list[dict], registry: list[dict] | None = None, top: int = 10,
            since: dt.date | None = None) -> dict:
    """Сводка для руководителя по записям спроса и реестру ТКП (ТКП — с даты since, если задана)."""
    kinds = collections.Counter()
    kinds_qty = collections.Counter()
    kinds_closed = collections.Counter()
    asked = collections.Counter()
    offered = collections.Counter()
    unmet = collections.Counter()
    reqs = collections.Counter()
    positions = closed = 0
    for rec in records:
        for p in rec.get("positions", []):
            positions += 1
            kind = p.get("kind") or "вид не определён"
            kinds[kind] += 1
            kinds_qty[kind] += int(p.get("qty") or 1)
            if p.get("asked_model"):
                asked[p["asked_model"]] += 1
            if p.get("status") in OFFERED:
                closed += 1
                kinds_closed[kind] += 1
                if p.get("model"):
                    offered[p["model"]] += int(p.get("qty") or 1)
            elif p.get("status") in UNMET:
                why = UNMET[p["status"]] + (f": {p['unmet']}" if p.get("unmet") else "")
                unmet[(kind, why)] += 1
            for r in p.get("reqs", []):
                reqs[f"{kind.lower()}: {r}"] += 1
    registry = [r for r in (registry or []) if r.get("status") != "заменён"
                and (not since or r.get("date", "")[:10] >= since.isoformat())]
    lost = [r for r in registry if r.get("status") == "проигран"]
    won = [r for r in registry if r.get("status") == "выигран"]
    reasons = collections.Counter(_lost_reason(r) for r in lost)
    by_model = collections.defaultdict(lambda: [0, 0])
    for r in won:
        for m in r.get("models", []):
            by_model[m][0] += 1
    for r in lost:
        for m in r.get("models", []):
            by_model[m][1] += 1
    return {
        "requests": len(records), "positions": positions, "closed": closed,
        "closed_share": round(100 * closed / positions) if positions else None,
        "kinds": [(k, n, kinds_qty[k], kinds_closed[k]) for k, n in kinds.most_common(top)],
        "asked": asked.most_common(top),
        "offered": offered.most_common(top),
        "unmet": [(k, why, n) for (k, why), n in unmet.most_common(top)],
        "reqs": reqs.most_common(top),
        "won": len(won), "lost": len(lost), "lost_reasons": reasons.most_common(top),
        "by_model": sorted(((m, w, lo) for m, (w, lo) in by_model.items()), key=lambda x: (-(x[1] + x[2]), x[0]))[:top],
    }

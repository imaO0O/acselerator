"""Пробелы в документации: вопросы сотрудников, на которые в документах и учёте не нашлось ответа.

Похожие формулировки («сколько стоит поверка осциллографа», «цена поверки осциллографа») собираются
в одну тему по общим основам слов. По теме видно, сколько раз её спрашивали, у каких ролей и когда
последний раз, — это список того, что стоит дописать в регламенты или добавить в базу знаний.

Пробел закрывается сам, когда на тот же вопрос под той же ролью агент теперь отвечает — последняя попытка
закончилась ответом (например, после загрузки регламента): тема уходит в «закрытые» с датой закрытия.
Если ответ есть только под другими ролями, это не пробел в документах, а права доступа — роли показываются.
"""
from collections import Counter

from .text import tokenize

UNANSWERED = {"no_data", "model_refused", "unverified"}
SERVICE_USERS = {None, "", "eval"}       # прогоны эталона и проверки перед показом
SIMILAR = 0.5                            # доля общих основ слов, при которой вопросы — одна тема
CLOSED_SIMILAR = 0.8                     # ответ на почти тот же вопрос, а не на более общий, — пробел закрыт


def _words(record: dict) -> set[str]:
    return set(tokenize(record.get("asked") or record.get("question") or ""))


def _similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _answering_since(words: list[set[str]], attempts: list[tuple[dict, set[str]]]) -> dict[str, str]:
    """По ролям: с какого времени агент отвечает на вопрос темы (роли, где последняя попытка — отказ, не входят).

    Считаются все попытки, в том числе проверки администратора и прогоны эталона: одиночный ответ,
    после которого агент снова не нашёл ответа, пробел не закрывает."""
    since: dict[str, str] = {}
    same = [a for a, w in attempts if any(_similar(w, x) >= CLOSED_SIMILAR for x in words)]
    for a in sorted(same, key=lambda a: a.get("time", "")):
        role = a.get("role") or "—"
        if a.get("status") != "answer":
            since.pop(role, None)
        else:
            since.setdefault(role, a.get("time", ""))
    return since


def find_gaps(records: list[dict]) -> tuple[list[dict], dict]:
    """Темы без ответа (самые частые — первыми, закрытые — в конце) и общая сводка по вопросам сотрудников."""
    people = [r for r in records if r.get("user") not in SERVICE_USERS and r.get("route") != "help"]
    attempts = [(r, _words(r)) for r in records if r.get("route") != "help"]
    misses = [r for r in people if r.get("status") in UNANSWERED and _words(r)]
    topics: list[dict] = []
    for r in sorted(misses, key=lambda x: x.get("time", "")):
        words = _words(r)
        best = max(topics, key=lambda t: len(words & t["words"]) / len(words | t["words"]), default=None)
        if best is not None and len(words & best["words"]) / len(words | best["words"]) >= SIMILAR:
            best["records"].append(r)
            best["words"] |= words
        else:
            topics.append({"words": set(words), "records": [r]})
    out = []
    for t in topics:
        recs = t["records"]
        # уточнение в диалоге («А если ночью?») понятно только вместе с предыдущим — показываем, как его понял агент
        phrasings = Counter((r.get("asked") or r["question"]).strip() for r in recs)
        role = max(recs, key=lambda r: r.get("time", "")).get("role") or "—"
        since = _answering_since([_words(r) for r in recs], attempts)
        out.append({"question": phrasings.most_common(1)[0][0], "count": len(recs),
                    "users": len({r.get("user") for r in recs}), "roles": sorted({r.get("role") or "—" for r in recs}),
                    "last": max(r.get("time", "") for r in recs),
                    "other": [q for q, _ in phrasings.most_common()[1:4]], "closed": since.get(role, ""),
                    "other_roles": sorted(r for r in since if r != role)})
    out.sort(key=lambda g: g["last"], reverse=True)      # при равном числе — недавние выше
    out.sort(key=lambda g: g["count"], reverse=True)
    out.sort(key=lambda g: bool(g["closed"]))             # открытые — первыми
    closed = sum(1 for g in out if g["closed"])
    summary = {"questions": len(people), "unanswered": len(misses), "topics": len(out) - closed, "closed": closed}
    return out, summary

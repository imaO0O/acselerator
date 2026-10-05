"""Пробелы в документации: вопросы сотрудников, на которые в документах и учёте не нашлось ответа.

Похожие формулировки («сколько стоит поверка осциллографа», «цена поверки осциллографа») собираются
в одну тему по общим основам слов. По теме видно, сколько раз её спрашивали, у каких ролей и когда
последний раз, — это список того, что стоит дописать в регламенты или добавить в базу знаний.
"""
from collections import Counter

from .text import tokenize

UNANSWERED = {"no_data", "model_refused", "unverified"}
SERVICE_USERS = {None, "", "eval"}       # прогоны эталона и проверки перед показом
SIMILAR = 0.5                            # доля общих основ слов, при которой вопросы — одна тема


def _words(record: dict) -> set[str]:
    return set(tokenize(record.get("asked") or record.get("question") or ""))


def find_gaps(records: list[dict]) -> tuple[list[dict], dict]:
    """Темы без ответа (самые частые — первыми) и общая сводка по вопросам сотрудников."""
    people = [r for r in records if r.get("user") not in SERVICE_USERS and r.get("route") != "help"]
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
        out.append({"question": phrasings.most_common(1)[0][0], "count": len(recs),
                    "users": len({r.get("user") for r in recs}), "roles": sorted({r.get("role") or "—" for r in recs}),
                    "last": max(r.get("time", "") for r in recs),
                    "other": [q for q, _ in phrasings.most_common()[1:4]]})
    out.sort(key=lambda g: g["last"], reverse=True)      # при равном числе — недавние выше
    out.sort(key=lambda g: g["count"], reverse=True)
    summary = {"questions": len(people), "unanswered": len(misses), "topics": len(out)}
    return out, summary

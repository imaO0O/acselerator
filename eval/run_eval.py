"""Прогон эталонного набора вопросов и подсчёт метрик.

    python eval/run_eval.py                                  # eval/questions.yaml → results/latest.*
    python eval/run_eval.py --questions eval/blind.yaml --out blind   # «слепой» набор команды
    python eval/run_eval.py --threshold 0.05

Результаты: eval/results/<out>.md (таблица для презентации), <out>.csv, <out>.jsonl.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import demo_db  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402

RESULTS = ROOT / "eval" / "results"
GROUPS = ["основной", "новый", "перефразировка", "опечатки", "ловушки", "ловушки-2", "ловушки-3"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("\xa0", " ")
    s = re.sub(r"(\d)\.(\d)", r"\1,\2", s)       # 2.5 → 2,5
    s = re.sub(r"(\d)\s+%", r"\1%", s)            # 10 % → 10%
    return re.sub(r"\s+", " ", s)


def expand_dates(item: str, shift: timedelta) -> str:
    """«date:2026-11-12» → та же дата демо-базы после сдвига, в трёх написаниях."""
    def one(alt: str) -> str:
        if not alt.startswith("date:"):
            return alt
        d = date.fromisoformat(alt[5:]) + shift
        return f"{d.isoformat()}|{d:%d.%m.%Y}|{d.day} {MONTHS[d.month - 1]}"
    return "|".join(one(a) for a in item.split("|"))


def has_facts(answer: str, must: list[str]) -> bool:
    text = norm(answer)
    return all(any(norm(alt).replace(" %", "%") in text for alt in item.split("|")) for item in must)


def has_none(answer: str, banned: list[str]) -> bool:
    text = norm(answer)
    return not any(norm(b) in text for b in banned)


def clause_tuple(c: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", c))


def clause_covers(chunk_clause: str | None, gold: str) -> bool:
    """Покрывает ли пункт или диапазон фрагмента («4.1–4.3») эталонный пункт («4.3»)."""
    if not chunk_clause:
        return False
    parts = re.split(r"[–-]", chunk_clause)
    lo, hi = clause_tuple(parts[0]), clause_tuple(parts[-1])
    g = clause_tuple(gold)
    if g[:len(lo)] == lo or g[:len(hi)] == hi:
        return True
    if lo[:len(g)] == g or hi[:len(g)] == g:   # эталон — раздел «3», фрагмент — пункты 3.1–3.4
        return True
    return lo <= g <= hi if len(lo) == len(g) == len(hi) else lo <= g[:len(lo)] <= hi


def source_ok(sources: list[dict], gold: dict) -> bool:
    docs = gold["doc"] if isinstance(gold["doc"], list) else [gold["doc"]]
    for s in sources:
        if not s["cited"] or s["doc_id"] not in docs:
            continue
        if "clause" not in gold or clause_covers(s["clause"], gold["clause"]):
            return True
    return False


def ask_with_history(agent: Agent, q: dict, role: str):
    """Предыдущие вопросы диалога задаются по-настоящему — как в чате."""
    context = []
    for prev in q.get("history", []):
        r = agent.ask(prev, role=role, history=context, user="eval")
        context.append({"question": r.asked or prev, "text": r.text,
                        "docs": [s["doc_id"] for s in r.sources if s["cited"]]})
    return agent.ask(q["question"], role=role, history=context or None, user="eval")   # в журнале — отдельно


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.yaml"), help="файл с вопросами")
    ap.add_argument("--out", default="latest", help="имя файлов результата в eval/results")
    ap.add_argument("--threshold", type=float, help="порог отказа переранжировщика")
    ap.add_argument("--role", default="Администратор")
    ap.add_argument("--only", help="только эти вопросы через запятую, например D01,D02")
    args = ap.parse_args()
    if args.threshold is not None:
        settings.rerank_threshold = args.threshold

    if settings.db_path == demo_db.DB:
        demo_db.ensure_current()
    shift = demo_db.offset()
    questions = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only.split(",")]
    agent = Agent()
    rows = []
    for q in questions:
        must = [expand_dates(m, shift) for m in q.get("must_include", [])]
        r = ask_with_history(agent, q, args.role)
        evidence = r.text + " " + json.dumps(r.rows, ensure_ascii=False)
        row = {"id": q["id"], "group": q.get("group", "основной"), "type": q["type"], "question": q["question"],
               "asked": r.asked or "", "route": r.route, "status": r.status, "answer": r.text,
               "seconds": r.seconds, "best_score": r.stats.get("best_score"),
               "cited": "; ".join(s["label"] for s in r.sources if s["cited"]), "sql": r.sql or "",
               "db_attempt": f"{r.stats.get('db_status')}: {r.stats.get('db_sql')}" if "db_status" in r.stats else ""}
        if q["type"] in ("doc", "both"):
            row["facts_ok"] = r.answered and has_facts(evidence, must) and has_none(r.text, q.get("must_exclude", []))
            row["source_ok"] = r.answered and ("source" not in q or source_ok(r.sources, q["source"]))
            row["correct"] = row["facts_ok"] and row["source_ok"] and (q["type"] == "doc" or r.route == "both")
        elif q["type"] == "none":
            row["correct"] = not r.answered
        elif q["type"] == "clarify":
            row["correct"] = r.status == "clarify"
        elif q["type"] == "info":
            row["correct"] = r.status == "info" and r.route == "help"
        else:
            row["correct"] = (r.answered and r.route == "database" and has_facts(evidence, must)
                              and has_none(r.text, q.get("must_exclude", [])))
        rows.append(row)
        mark = "+" if row["correct"] else "-"
        print(f"{mark} {q['id']} [{r.route}/{r.status}, {r.seconds}s] {q['question'][:70]}")

    doc = [r for r in rows if r["type"] == "doc"]
    none = [r for r in rows if r["type"] == "none"]
    db = [r for r in rows if r["type"] == "db"]
    both = [r for r in rows if r["type"] == "both"]
    clarify = [r for r in rows if r["type"] == "clarify"]
    info = [r for r in rows if r["type"] == "info"]
    answered = [r for r in doc + none if r["status"] == "answer"]
    count = lambda items, key="correct": f"{sum(bool(r.get(key)) for r in items)} из {len(items)}"  # noqa: E731
    m = {
        "Правильных ответов (факты и источник верны)": count(doc),
        "Ответов с верными фактами": count(doc, "facts_ok"),
        "Ответов со ссылкой на верный документ и пункт": count(doc, "source_ok"),
        "Ответов со ссылкой на источник (среди всех данных ответов)":
            f"{sum(1 for r in answered if r['cited'])} из {len(answered)}",
        "Верных отказов на вопросы без ответа": count(none),
        "Ложных ответов на вопросы без ответа": f"{sum(not r['correct'] for r in none)} из {len(none)}",
        "Неверных ответов, выданных как ответ":
            f"{sum(1 for r in doc if r['status'] == 'answer' and not r['facts_ok'])} из {len(doc)}",
        "Ответов по учётной базе": count(db),
        "Ответов по базе и документам вместе": count(both),
        "Встречных вопросов на слишком общий вопрос": count(clarify),
        "Справок на разговорные вопросы («привет», «что ты умеешь»)": count(info),
        "Время ответа, медиана": f"{statistics.median(r['seconds'] for r in rows):.1f} с",
        "Время ответа, максимум": f"{max(r['seconds'] for r in rows):.1f} с",
    }
    m = {k: v for k, v in m.items() if not v.endswith(" из 0")}

    groups = []
    for g in [g for g in GROUPS if any(r["group"] == g for r in rows)]:
        cells = []
        for items in (doc, none, db, both + clarify + info):
            gi = [r for r in items if r["group"] == g]
            cells.append(count(gi) if gi else "—")
        groups.append(f"| {g} | " + " | ".join(cells) + " |")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = [f"# Результаты на эталонном наборе ({stamp})", "",
          f"Набор: {Path(args.questions).name}, вопросов: {len(rows)}; модель: {settings.llm_model}; "
          f"порог отказа: {settings.rerank_threshold}; роль: {args.role}", "",
          "| Метрика | Значение |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in m.items()]
    md += ["", "По группам вопросов:", "",
           "| Группа | Верные ответы | Верные отказы | Учётная база | База + документы, уточнения, справки |",
           "|---|---|---|---|---|"] + groups
    md += ["", "| № | Группа | Вопрос | Итог | Статус | Время, с | Источник |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['group']} | {r['question']} | {'верно' if r['correct'] else 'ошибка'} | {r['status']} "
                  f"| {r['seconds']} | {r['cited'] or (r['sql'][:60].replace(chr(10), ' ') if r['sql'] else '—')} |")
    (RESULTS / f"{args.out}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with open(RESULTS / f"{args.out}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["facts_ok", "source_ok"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(RESULTS / f"{args.out}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print()
    for k, v in m.items():
        print(f"{k}: {v}")
    print(chr(10).join(groups))


if __name__ == "__main__":
    main()

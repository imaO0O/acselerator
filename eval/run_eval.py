"""Прогон эталонного набора вопросов и подсчёт метрик.

    python eval/run_eval.py                 # все вопросы
    python eval/run_eval.py --threshold 0.05

Результаты: eval/results/latest.md (таблица для презентации), latest.csv, latest.jsonl.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402

RESULTS = ROOT / "eval" / "results"


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("\xa0", " ")
    s = re.sub(r"(\d)\.(\d)", r"\1,\2", s)       # 2.5 → 2,5
    s = re.sub(r"(\d)\s+%", r"\1%", s)            # 10 % → 10%
    return re.sub(r"\s+", " ", s)


def has_facts(answer: str, must: list[str]) -> bool:
    text = norm(answer)
    return all(any(norm(alt).replace(" %", "%") in text for alt in item.split("|")) for item in must)


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
    for s in sources:
        if not s["cited"] or s["doc_id"] != gold["doc"]:
            continue
        if "clause" not in gold or clause_covers(s["clause"], gold["clause"]):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, help="порог отказа переранжировщика")
    ap.add_argument("--role", default="Администратор")
    args = ap.parse_args()
    if args.threshold is not None:
        settings.rerank_threshold = args.threshold

    questions = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    agent = Agent()
    rows = []
    for q in questions:
        r = agent.ask(q["question"], role=args.role)
        row = {"id": q["id"], "group": q.get("group", "основной"), "type": q["type"], "question": q["question"], "route": r.route,
               "status": r.status, "answer": r.text, "seconds": r.seconds,
               "best_score": r.stats.get("best_score"),
               "cited": "; ".join(s["label"] for s in r.sources if s["cited"]), "sql": r.sql or ""}
        if q["type"] == "doc":
            row["facts_ok"] = r.answered and has_facts(r.text, q["must_include"])
            row["source_ok"] = r.answered and source_ok(r.sources, q["source"])
            row["correct"] = row["facts_ok"] and row["source_ok"]
        elif q["type"] == "none":
            row["correct"] = not r.answered
        else:
            evidence = r.text + " " + json.dumps(r.rows, ensure_ascii=False)
            row["correct"] = r.answered and r.route == "database" and has_facts(evidence, q["must_include"])
        rows.append(row)
        mark = "+" if row["correct"] else "-"
        print(f"{mark} {q['id']} [{r.route}/{r.status}, {r.seconds}s] {q['question'][:70]}")

    doc = [r for r in rows if r["type"] == "doc"]
    none = [r for r in rows if r["type"] == "none"]
    db = [r for r in rows if r["type"] == "db"]
    answered = [r for r in doc + none if r["status"] == "answer"]
    m = {
        "Правильных ответов (факты и источник верны)": f"{sum(r['correct'] for r in doc)} из {len(doc)}",
        "Ответов с верными фактами": f"{sum(r['facts_ok'] for r in doc)} из {len(doc)}",
        "Ответов со ссылкой на верный документ и пункт": f"{sum(r['source_ok'] for r in doc)} из {len(doc)}",
        "Ответов со ссылкой на источник (среди всех данных ответов)":
            f"{sum(1 for r in answered if r['cited'])} из {len(answered)}",
        "Верных отказов на вопросы без ответа": f"{sum(r['correct'] for r in none)} из {len(none)}",
        "Ложных ответов на вопросы без ответа": f"{sum(not r['correct'] for r in none)} из {len(none)}",
        "Неверных ответов, выданных как ответ": f"{sum(1 for r in doc if r['status'] == 'answer' and not r['facts_ok'])} из {len(doc)}",
        "Ответов по учётной базе": f"{sum(r['correct'] for r in db)} из {len(db)}",
        "Время ответа, медиана": f"{statistics.median(r['seconds'] for r in rows):.1f} с",
        "Время ответа, максимум": f"{max(r['seconds'] for r in rows):.1f} с",
    }

    groups = []
    for g in ["основной", "новый", "перефразировка", "опечатки"]:
        gd = [r for r in doc if r["group"] == g]
        gn = [r for r in none if r["group"] == g]
        gb = [r for r in db if r["group"] == g]
        cells = [f"{sum(r['correct'] for r in gd)} из {len(gd)}" if gd else "—",
                 f"{sum(r['correct'] for r in gn)} из {len(gn)}" if gn else "—",
                 f"{sum(r['correct'] for r in gb)} из {len(gb)}" if gb else "—"]
        groups.append(f"| {g} | " + " | ".join(cells) + " |")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = [f"# Результаты на эталонном наборе ({stamp})", "",
          f"Модель: {settings.llm_model}; порог отказа: {settings.rerank_threshold}; роль: {args.role}", "",
          "| Метрика | Значение |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in m.items()]
    md += ["", "По группам вопросов:", "", "| Группа | Верные ответы | Верные отказы | Учётная база |", "|---|---|---|---|"] + groups
    md += ["", "| № | Группа | Вопрос | Итог | Статус | Время, с | Источник |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['group']} | {r['question']} | {'верно' if r['correct'] else 'ошибка'} | {r['status']} "
                  f"| {r['seconds']} | {r['cited'] or (r['sql'][:60] if r['sql'] else '—')} |")
    (RESULTS / "latest.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with open(RESULTS / "latest.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["facts_ok", "source_ok"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(RESULTS / "latest.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print()
    for k, v in m.items():
        print(f"{k}: {v}")
    print(chr(10).join(groups))


if __name__ == "__main__":
    main()

"""Проверка ветки перед слиянием в main — одной командой на компьютере с моделями.

    python scripts/check_branch.py                              автотесты, check_demo, все наборы, сравнение с main
    python scripts/check_branch.py --baseline _laptop3050_e10   сравнить с прогонами ноутбука (*_laptop3050_e10.csv)
    python scripts/check_branch.py --only tkp,jury              только эти наборы
    python scripts/check_branch.py --compare-only               не прогонять заново — сравнить уже сделанные прогоны

Перед запуском — серверы моделей (scripts\\start_servers.bat). Прогоны пишутся в eval/results/check_*.{md,csv}
(в git не попадают) — результаты main не перезаписываются. Отчёт — eval/results/check_branch.md: по каждому
набору было / стало, какие вопросы сломались и какие починились, новые вопросы, медиана времени ответа.
Код выхода 0 — автотесты и показ в порядке, сломанных вопросов нет: ветку можно сливать.
"""
import argparse
import csv
import io
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
SETS = {"latest": "eval/questions.yaml", "jury": "eval/jury.yaml", "roles": "eval/roles.yaml",
        "spravochnik": "eval/spravochnik.yaml", "tkp": "eval/tkp.yaml", "testers": "eval/testers.yaml"}
TITLES = {"latest": "эталон", "jury": "вопросы «с ходу»", "roles": "роли", "spravochnik": "справочник",
          "tkp": "письма → ТКП", "testers": "приёмы участника 2"}


def parse(text: str | None) -> dict[str, dict] | None:
    """CSV прогона → {id: {correct, seconds, question}}; None — прогона нет."""
    if text is None:
        return None
    rows = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        rows[r["id"]] = {"correct": r.get("correct") == "True", "question": " ".join(r.get("question", "").split()),
                         "seconds": float(r.get("seconds") or 0)}
    return rows


def baseline(name: str, ref: str, suffix: str) -> str | None:
    """Прогон из ветки ref (по умолчанию main): eval/results/<набор><suffix>.csv."""
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:eval/results/{name}{suffix}.csv"],
                          capture_output=True)
    return done.stdout.decode("utf-8-sig") if done.returncode == 0 else None


def compare(base: dict | None, new: dict) -> dict:
    ok = sum(r["correct"] for r in new.values())
    out = {"now": f"{ok} из {len(new)}", "was": "—", "broken": [], "fixed": [], "added": [], "removed": [],
           "median_now": statistics.median(r["seconds"] for r in new.values()) if new else 0.0, "median_was": None}
    if base is None:
        out["added"] = [(i, r["question"], r["correct"]) for i, r in new.items()]
        return out
    out["was"] = f"{sum(r['correct'] for r in base.values())} из {len(base)}"
    out["median_was"] = statistics.median(r["seconds"] for r in base.values()) if base else None
    for i, r in new.items():
        if i not in base:
            out["added"].append((i, r["question"], r["correct"]))
        elif base[i]["correct"] and not r["correct"]:
            out["broken"].append((i, r["question"]))
        elif not base[i]["correct"] and r["correct"]:
            out["fixed"].append((i, r["question"]))
    out["removed"] = [(i, r["question"]) for i, r in base.items() if i not in new]
    return out


def report(results: dict[str, dict], steps: dict[str, bool], ref: str, suffix: str) -> tuple[str, bool]:
    broken = sum(len(c["broken"]) for c in results.values())
    failed_new = sum(1 for c in results.values() for _, _, ok in c["added"] if not ok)
    clean = all(steps.values()) and broken == 0
    lines = [f"# Проверка ветки перед слиянием ({time.strftime('%Y-%m-%d %H:%M')})", "",
             f"Сравнение с `{ref}` (прогоны `*{suffix}.csv`). Итог: **{'можно сливать' if clean else 'есть проблемы'}**.",
             ""]
    lines += [f"- {name}: {'пройдено' if ok else '**не пройдено**'}" for name, ok in steps.items()]
    lines += ["", "| Набор | Было | Стало | Сломалось | Починилось | Новых вопросов (неверно) | Медиана, с |",
              "|---|---|---|---|---|---|---|"]
    for name, c in results.items():
        median = (f"{c['median_was']:.1f} → {c['median_now']:.1f}" if c["median_was"] is not None
                  else f"{c['median_now']:.1f}").replace(".", ",")
        new_bad = sum(1 for _, _, ok in c["added"] if not ok)
        lines.append(f"| {TITLES.get(name, name)} | {c['was']} | {c['now']} | {len(c['broken'])} | {len(c['fixed'])} | "
                     f"{len(c['added'])} ({new_bad}) | {median} |")
    for name, c in results.items():
        if c["broken"] or c["fixed"] or c["removed"] or any(not ok for _, _, ok in c["added"]):
            lines += ["", f"## {TITLES.get(name, name)}"]
            lines += [f"- **сломался** {i}: {q[:150]}" for i, q in c["broken"]]
            lines += [f"- починился {i}: {q[:150]}" for i, q in c["fixed"]]
            lines += [f"- новый, **неверно** {i}: {q[:150]}" for i, q, ok in c["added"] if not ok]
            lines += [f"- убран из набора {i}: {q[:150]}" for i, q in c["removed"]]
    if failed_new:
        lines += ["", f"Новые вопросы с ошибкой ({failed_new}) — не регрессия, но их стоит разобрать до слияния."]
    lines += ["", "Ответы целиком — eval/results/check_<набор>.md."]
    return "\n".join(lines) + "\n", clean


def run(cmd: list[str], title: str) -> bool:
    print(f"\n=== {title}: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка ветки перед слиянием в main")
    ap.add_argument("--ref", default="main", help="с какой веткой сравнивать (по умолчанию main)")
    ap.add_argument("--baseline", default="", help="суффикс прогонов для сравнения, например _laptop3050_e10")
    ap.add_argument("--only", help="наборы через запятую: " + ", ".join(SETS))
    ap.add_argument("--compare-only", action="store_true", help="только сравнить уже сделанные прогоны check_*")
    args = ap.parse_args()
    names = [n for n in (args.only.split(",") if args.only else SETS) if n in SETS]
    py = sys.executable
    steps: dict[str, bool] = {}
    if not args.compare_only:
        sys.path.insert(0, str(ROOT))
        from kbagent import llm
        down = [k for k, ok in llm.health().items() if not ok]
        if down:
            print(f"Серверы моделей не отвечают: {', '.join(down)}. Запустите scripts\\start_servers.bat и повторите.")
            return 2
        steps["автотесты"] = run([py, "-m", "pytest", "-q", "tests"], "автотесты")
        steps["check_demo (сценарий показа)"] = run([py, "scripts/check_demo.py", "--clean"], "сценарий показа")
        for n in names:
            steps[f"прогон «{TITLES[n]}»"] = run([py, "eval/run_eval.py", "--questions", SETS[n], "--out", f"check_{n}"],
                                                 TITLES[n])
    results = {}
    for n in names:
        path = RESULTS / f"check_{n}.csv"
        if path.exists():
            results[n] = compare(parse(baseline(n, args.ref, args.baseline)), parse(path.read_text(encoding="utf-8-sig")))
    text, clean = report(results, steps, args.ref, args.baseline)
    (RESULTS / "check_branch.md").write_text(text, encoding="utf-8")
    print("\n" + text)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())

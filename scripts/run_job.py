"""Запуск фоновой задачи с записью итога для вкладки «Система» (kbagent/jobs.py). Его вызывает Планировщик
заданий Windows — задачи регистрирует scripts/install_tasks.bat; вручную — для проверки.

    python scripts/run_job.py backup    резервная копия
    python scripts/run_job.py mail      почта отдела
    python scripts/run_job.py folder    папка документов отдела
    python scripts/run_job.py 1c        цены из 1С
    python scripts/run_job.py --plan    задачи, их период и настроены ли (для install_tasks)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import jobs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Фоновая задача агента")
    ap.add_argument("job", nargs="?", choices=list(jobs.JOBS))
    ap.add_argument("--plan", action="store_true", help="задачи, период в минутах и настроены ли")
    args = ap.parse_args()
    if args.plan:
        for job in jobs.JOBS.values():
            print(f"{job.key}|{job.every}|{int(jobs.configured(job.key))}|{job.title}")
        return 0
    if not args.job:
        ap.error("укажите задачу")
    if not jobs.configured(args.job):
        print(f"{jobs.JOBS[args.job].title}: не настроено (data/settings.env) — пропущено")
        return 0
    rec = jobs.run(args.job)
    print(rec["output"] or f"код {rec['code']}")   # под pythonw вывода нет — итог в logs/jobs
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

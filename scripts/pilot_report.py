"""Отчёт о пилотной эксплуатации в Word по журналам агента (kbagent/pilot_report.py).

    python scripts/pilot_report.py --since 01.11.2026 --until 30.11.2026 --out otchet_pilot.docx
    python scripts/pilot_report.py                      последние 30 дней, файл otchet_pilot_<период>.docx

То же — кнопка «Сформировать отчёт (Word)» на вкладке «Администрирование» → «Эффект».
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import pilot_report, tkp_registry  # noqa: E402

TITLE = "AI-агент для поиска ответов по внутренней документации и базам данных предприятия"


def day(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d.%m.%Y").date()


def main() -> int:
    ap = argparse.ArgumentParser(description="Отчёт о пилоте в Word")
    today = dt.date.today()
    ap.add_argument("--since", type=day, default=today - dt.timedelta(days=30), help="ДД.ММ.ГГГГ")
    ap.add_argument("--until", type=day, default=today, help="ДД.ММ.ГГГГ")
    ap.add_argument("--company", default=None, help="заказчик (по умолчанию — из условий ТКП)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    data = pilot_report.collect(args.since, args.until)
    company = args.company if args.company is not None else tkp_registry.load_settings().get("company", "")
    out = args.out or Path(f"otchet_pilot_{args.since:%Y%m%d}_{args.until:%Y%m%d}.docx")
    out.write_bytes(pilot_report.report_docx(data, company, TITLE))
    print(f"{out}: вопросов {data['questions']}, с ответом {data['answered']}, со ссылкой {data['cited']}")
    for line in pilot_report.goal_lines(data["effect"]):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

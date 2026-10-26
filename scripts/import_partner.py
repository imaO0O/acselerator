"""Загрузка заполненного партнёром шаблона (docs/pilot/paket/Shablon_dannyh_dlya_pilota.xlsx).

    python scripts/import_partner.py Shablon.xlsx --docs data/raw/partner
        реестр → data/raw/partner/catalog.yaml, вопросы для настройки → eval/partner.yaml,
        справочник и прайс → data/partner_import/ (CSV для вкладки «Справочник» администрирования)
    python scripts/import_partner.py Priemka.xlsx --docs data/raw/partner --questions eval/priemka.yaml --prefix A --only-questions
        закрытый приёмочный набор — только вопросы, в отдельный файл; прогон без правок агента

Документы партнёра кладутся в --docs до загрузки: реестр проверяет, что каждый файл на месте. Ошибочные строки
не загружаются; список ошибок с листом и строкой Excel — на экране и в файле «…_oshibki.csv» рядом с шаблоном,
его можно отправить партнёру. Данные партнёра в git не попадают (.gitignore).

Дальше: python scripts/build_index.py и python eval/run_eval.py --questions eval/partner.yaml --out partner
"""
import argparse
import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import pilot_data  # noqa: E402


def dump(path: Path, head: str, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Загрузка шаблона данных партнёра")
    ap.add_argument("workbook", type=Path, help="заполненная книга Excel")
    ap.add_argument("--docs", type=Path, default=ROOT / "data" / "raw" / "partner",
                    help="папка с документами партнёра — сюда же пишется catalog.yaml")
    ap.add_argument("--questions", type=Path, default=ROOT / "eval" / "partner.yaml", help="файл набора вопросов")
    ap.add_argument("--prefix", default="P", help="буква номеров вопросов: P — настройка, A — приёмка")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "partner_import", help="папка для справочника и прайса")
    ap.add_argument("--only-questions", action="store_true", help="только вопросы (закрытый приёмочный набор)")
    args = ap.parse_args()

    res = pilot_data.load(args.workbook, args.docs, args.prefix)
    for sheet, n in res.counts.items():
        print(f"{sheet}: загружено строк — {n}")
    source = f"# Загружено из «{args.workbook.name}» (scripts/import_partner.py). Данные партнёра — в git не добавлять.\n"
    if res.questions:
        dump(args.questions, source, {"questions": res.questions})
        print(f"Вопросы → {args.questions}")
    if not args.only_questions:
        if res.documents:
            dump(args.docs / "catalog.yaml", source, {"documents": res.documents})
            print(f"Реестр → {args.docs / 'catalog.yaml'}")
        for name, text in (("spravochnik.csv", res.spravochnik_csv), ("prajs.csv", res.prajs_csv)):
            if text:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / name).write_text(text, encoding="utf-8-sig")
                print(f"{name} → {args.out / name} (загрузить на вкладке «Справочник» администрирования)")
    for note in res.notes:
        print(f"Сделать вручную: {note}")
    if not res.errors:
        print("Ошибок нет.")
        return 0
    errors_path = args.workbook.with_name(args.workbook.stem + "_oshibki.csv")
    with open(errors_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Лист", "Строка", "Что исправить"])
        for e in res.errors:
            w.writerow([e["sheet"], e["row"], e["error"]])
    print(f"\nОшибок: {len(res.errors)} — строки не загружены. Список для партнёра: {errors_path}")
    for e in res.errors[:20]:
        print(f"  {e['sheet']}, строка {e['row']}: {e['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

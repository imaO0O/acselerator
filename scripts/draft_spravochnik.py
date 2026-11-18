"""Черновик справочника параметров из руководства (kbagent/spravochnik_draft.py).

    python scripts/draft_spravochnik.py --check                         сколько внесённых вручную значений программа
                                                                        находит сама (по всем руководствам справочника)
    python scripts/draft_spravochnik.py --doc osc_akip_4143 --out draft.csv
                                                                        черновик по руководству — в CSV для эксперта
    python scripts/draft_spravochnik.py --doc solder_cxg_h95 --models "CXG DES H95" --kind "Паяльная станция"

То же в интерфейсе: «Администрирование» → «Справочник» → «Черновик из руководства».
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import spravochnik, spravochnik_draft as draft  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.store import Index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Черновик справочника из руководства")
    ap.add_argument("--check", action="store_true", help="сверить со справочником, заполненным вручную")
    ap.add_argument("--doc", help="документ (id из реестра)")
    ap.add_argument("--models", help="модели через запятую (по умолчанию — из названия документа)")
    ap.add_argument("--kind", help="вид продукции (по умолчанию — из названия документа)")
    ap.add_argument("--out", type=Path, help="CSV для эксперта (по умолчанию — вывод на экран)")
    args = ap.parse_args()
    by_doc = defaultdict(list)
    for c in sorted(Index.load(settings.index_dir).chunks, key=lambda c: (c.doc_id, c.page_start or 0)):
        by_doc[c.doc_id].append(c)
    if args.check:
        values, _ = spravochnik.load()
        r = draft.check(by_doc, values)
        print(f"Руководств: {r['docs']}; значений в справочнике: {r['total']}; программа нашла сама: {r['found']} "
              f"({100 * r['found'] / max(r['total'], 1):.0f} %); предложено значений: {r['proposed']}")
        return 0
    if not args.doc or args.doc not in by_doc:
        print("Укажите --doc: " + ", ".join(sorted(by_doc)))
        return 1
    title = by_doc[args.doc][0].doc_title
    models = [m.strip() for m in args.models.split(",")] if args.models else draft.models_from_title(title)
    rows = draft.candidates(by_doc[args.doc], models, args.kind or draft.kind_of(title))
    columns = spravochnik.COLUMNS[:6] + ["Страница", "Цитата", "Другие значения в тексте"]
    out = open(args.out, "w", encoding="utf-8-sig", newline="") if args.out else sys.stdout
    w = csv.writer(out, delimiter=";")
    w.writerow(columns)
    for r in rows:
        w.writerow([args.doc if c == "Документ" else r.get(c, "") for c in columns])
    if args.out:
        out.close()
        print(f"{args.out}: строк {len(rows)} — проверьте по цитатам и загрузите в «Справочник»")
    return 0


if __name__ == "__main__":
    sys.exit(main())

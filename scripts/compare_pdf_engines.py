"""Замена PyMuPDF (AGPL-3.0, Э8-09): совпадают ли фрагменты корпуса при чтении PDF через pypdfium2.

    python scripts/compare_pdf_engines.py           — по каждому PDF: фрагментов у PyMuPDF и pypdfium2, совпало
    python scripts/compare_pdf_engines.py --show    — и первое расхождение в каждом документе

Фрагмент совпадает, если у него тот же пункт, заголовок, страницы и текст. Пока совпадают не все, замена
движка меняет поиск и ответы, и после неё нужен прогон всех наборов на компьютере с моделями. pypdfium2 —
только для этой проверки (requirements-dev.txt).

Проверка 26.10.2026: совпало 385 из 2065. Демо-PDF — целиком; в руководствах и приказах — другой порядок блоков
в таблицах и оглавлениях, пробелы и переносы.
"""
import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pypdfium2 as pdfium  # noqa: E402

from kbagent import ingest  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.text import join_hyphenated, normalize  # noqa: E402

PAGE_NO_RE = re.compile(r"\d{1,4}|-\s*\d{1,4}\s*-|(?:Страница|Стр\.|Лист)\s*\d{1,4}(?:\s*(?:из|/)\s*\d{1,4})?", re.I)


def pdfium_text(page) -> str:
    """Текст страницы; U+FFFE — перенос в конце строки, который pdfium склеивает: как мягкий перенос у PyMuPDF."""
    tp = page.get_textpage()
    text = tp.get_text_range()
    tp.close()
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("￾", "\xad\n")


def pdf_pages_pdfium(path: Path, stats: dict | None = None) -> list[tuple[int, list[str]]]:
    """То же, что ingest._pdf_pages, но через pypdfium2 (без распознавания сканов)."""
    doc = pdfium.PdfDocument(str(path))
    pages = []
    for i in range(len(doc)):
        lines = [ln.strip() for ln in normalize(pdfium_text(doc[i])).split("\n")]
        pages.append((i + 1, [ln for ln in lines if ln]))
    doc.close()
    boiler: set[str] = set()
    if len(pages) >= 4:
        freq = Counter(ln for _, lines in pages for ln in set(lines) if len(ln) < 80)
        boiler = {ln for ln, c in freq.items() if c >= max(3, 0.3 * len(pages))}
    return [(no, join_hyphenated([ln for ln in lines if ln not in boiler and not PAGE_NO_RE.fullmatch(ln)]))
            for no, lines in pages]


def key(c) -> tuple:
    return c.clause, c.heading, c.page_start, c.page_end, c.text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", action="store_true", help="показать первое расхождение в каждом документе")
    args = ap.parse_args()
    catalog = ingest.load_catalog(settings.raw_dir)
    mupdf_pages = ingest._pdf_pages
    same_total = total = 0
    for path in sorted(settings.raw_dir.rglob("*.pdf")):
        ingest._pdf_pages = mupdf_pages
        a = ingest.parse_document(path, catalog)
        ingest._pdf_pages = pdf_pages_pdfium
        b = ingest.parse_document(path, catalog)
        same = sum(1 for x, y in zip(a, b) if key(x) == key(y))
        same_total, total = same_total + same, total + max(len(a), len(b))
        print(f"{path.name:45} PyMuPDF {len(a):4}  pypdfium2 {len(b):4}  совпало {same}")
        if args.show:
            for x, y in zip(a, b):
                if key(x) == key(y):
                    continue
                print(f"   пункт {x.clause}, с. {x.page_start}–{x.page_end} → пункт {y.clause}, с. {y.page_start}–{y.page_end}")
                for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x.text, y.text).get_opcodes():
                    if op != "equal":
                        print(f"     {x.text[max(0, i1 - 20):i2 + 20]!r} → {y.text[max(0, j1 - 20):j2 + 20]!r}")
                break
    ingest._pdf_pages = mupdf_pages
    print(f"Совпало фрагментов: {same_total} из {total}")


if __name__ == "__main__":
    main()

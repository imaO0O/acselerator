"""Страница исходного PDF с выделенным фрагментом — наглядное доказательство,
что ответ взят из документа, а не придуман.

Текст фрагмента режется на короткие фразы, каждая ищется на странице PDF
и выделяется жёлтым, как маркером. Если фраза встречается на странице
несколько раз, берётся вхождение рядом с предыдущим выделением.
"""
from pathlib import Path

import pymupdf

from .config import settings

PHRASE_WORDS = 6


def source_path(doc_id: str) -> Path | None:
    for p in settings.raw_dir.rglob(f"{doc_id}.*"):
        if p.suffix.lower() in (".pdf", ".docx"):
            return p
    return None


def _phrases(text: str):
    words = text.split()
    for i in range(0, len(words), PHRASE_WORDS):
        phrase = " ".join(words[i:i + PHRASE_WORDS])
        if len(phrase) >= 12:
            yield phrase


def render_page(path: str, page_no: int, fragment: str, zoom: float = 1.6, crop: bool = False) -> tuple[bytes, int]:
    """PNG страницы с выделенным фрагментом и число найденных на ней фраз.
    crop=True — только полоса страницы вокруг выделения (во всю ширину), крупнее: её видно без прокрутки."""
    doc = pymupdf.open(path)
    page = doc[page_no - 1]
    found, last_y, marked = 0, None, []
    for phrase in _phrases(fragment):
        quads = page.search_for(phrase, quads=True)
        if not quads:
            continue
        # совпадение, перенесённое на следующую строку, возвращается несколькими
        # прямоугольниками — берём все части рядом с выбранным вхождением
        anchor = quads[0] if last_y is None else min(quads, key=lambda q: abs(q.rect.y0 - last_y))
        parts = [q for q in quads if anchor.rect.y0 - 2 <= q.rect.y0 <= anchor.rect.y0 + 30]
        for q in parts:
            page.add_highlight_annot(q)
            marked.append(q.rect)
        last_y = max(q.rect.y1 for q in parts)
        found += 1
    clip = None
    if crop and marked:
        top = max(page.rect.y0, min(r.y0 for r in marked) - 36)
        bottom = min(page.rect.y1, max(r.y1 for r in marked) + 36)
        clip = pymupdf.Rect(page.rect.x0, top, page.rect.x1, bottom)
    png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True, clip=clip).tobytes("png")
    doc.close()
    return png, found

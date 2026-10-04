"""Страница исходного PDF с выделенным фрагментом — наглядное доказательство,
что ответ взят из документа, а не придуман.

Слова фрагмента сопоставляются со словами страницы по порядку (как при сравнении
версий текста), совпавшие участки выделяются жёлтым построчно, как маркером.
Знаки препинания, регистр, «ё» и переносы слов на сравнение не влияют.
"""
import difflib
import re
from pathlib import Path

import pymupdf

from .config import settings

TOKEN_RE = re.compile(r"[a-zа-яё]+|\d+", re.I)
MIN_RUN = 3      # совпадение короче — случайное («в соответствии с»)
GAP_FILL = 2     # пропуск внутри выделения до стольких слов закрашивается: строка без «дыр»
MARGIN = 40      # поле вокруг выделения в вырезке, пт


def source_path(doc_id: str) -> Path | None:
    for p in settings.raw_dir.rglob(f"{doc_id}.*"):
        if p.suffix.lower() in (".pdf", ".docx"):
            return p
    return None


def _tokens(text: str) -> list[str]:
    return [t.lower().replace("ё", "е") for t in TOKEN_RE.findall(text)]


def _page_tokens(words: list) -> tuple[list[str], list[list[int]]]:
    """Слова страницы как последовательность токенов; у каждого — номера слов страницы.
    Слово, разорванное переносом в конце строки («опас-» + «ного»), склеивается."""
    toks, owners = [], []
    i = 0
    while i < len(words):
        w = words[i]
        parts = _tokens(w[4])
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (parts and nxt and re.search(r"[а-яёa-z][-­]$", w[4], re.I) and (nxt[5], nxt[6]) != (w[5], w[6])
                and re.match(r"[а-яёa-z]", nxt[4])):
            tail = _tokens(nxt[4])
            toks += parts[:-1] + [parts[-1] + tail[0]] + tail[1:]
            owners += [[i]] * (len(parts) - 1) + [[i, i + 1]] + [[i + 1]] * (len(tail) - 1)
            i += 2
            continue
        toks += parts
        owners += [[i]] * len(parts)
        i += 1
    return toks, owners


def _marked_words(words: list, fragment: str) -> set[int]:
    page_toks, owners = _page_tokens(words)
    frag = _tokens(fragment)
    if not page_toks or not frag:
        return set()
    sm = difflib.SequenceMatcher(None, page_toks, frag, autojunk=False)
    hit: list[int] = []
    for b in sm.get_matching_blocks():
        if b.size >= min(MIN_RUN, len(frag)):
            hit += range(b.a, b.a + b.size)
    gaps = [t for a, b in zip(hit, hit[1:]) if 1 < b - a <= GAP_FILL + 1 for t in range(a + 1, b)]
    marked: set[int] = set()
    for t in hit + gaps:
        marked.update(owners[t])
    # тире и другие знаки между выделенными словами — тоже, иначе в строке остаются разрывы
    marked |= {i for i in range(1, len(words) - 1)
               if not _tokens(words[i][4]) and i - 1 in marked and i + 1 in marked}
    return marked


def _line_rects(words: list, idx) -> dict:
    """Прямоугольники строк: (блок, строка) → общий прямоугольник слов из idx."""
    lines: dict = {}
    for i in idx:
        w = words[i]
        r = pymupdf.Rect(w[:4])
        key = (w[5], w[6])
        lines[key] = lines[key] | r if key in lines else r
    return lines


def render_page(path: str, page_no: int, fragment: str, zoom: float = 1.6, crop: bool = False) -> tuple[bytes, int]:
    """PNG страницы с выделенным фрагментом и число выделенных на ней слов.
    crop=True — только полоса страницы вокруг выделения (во всю ширину), крупнее: её видно без прокрутки.
    Край вырезки не режет строки пополам."""
    doc = pymupdf.open(path)
    page = doc[page_no - 1]
    words = page.get_text("words")
    marked = _marked_words(words, fragment)
    # выделение — непрерывными отрезками внутри строки
    runs: list[pymupdf.Rect] = []
    for i in sorted(marked):
        w = words[i]
        r = pymupdf.Rect(w[:4])
        prev = i - 1
        if runs and prev in marked and (words[prev][5], words[prev][6]) == (w[5], w[6]):
            runs[-1] = runs[-1] | r
        else:
            runs.append(r)
    for r in runs:
        page.add_highlight_annot(r)
    clip = None
    if crop and runs:
        area = pymupdf.Rect(runs[0])
        for r in runs[1:]:
            area |= r
        top = max(page.rect.y0, area.y0 - MARGIN)
        bottom = min(page.rect.y1, area.y1 + MARGIN)
        lines = list(_line_rects(words, range(len(words))).values())
        # строка колонки с выделением, разрезанная краем, входит целиком
        column = [ln for ln in lines if ln.x0 < area.x1 and ln.x1 > area.x0]
        top = max(page.rect.y0, min([top] + [ln.y0 - 3 for ln in column if ln.y0 < top < ln.y1]))
        bottom = min(page.rect.y1, max([bottom] + [ln.y1 + 3 for ln in column if ln.y0 < bottom < ln.y1]))
        # строки соседней колонки смещены и всё равно режутся краем — их половинки закрашиваются
        for ln in lines:
            if ln.y0 < top < ln.y1 or ln.y0 < bottom < ln.y1:
                page.draw_rect(ln + (-1, -1, 1, 1), color=None, fill=(1, 1, 1), overlay=True)
        clip = pymupdf.Rect(page.rect.x0, top, page.rect.x1, bottom)
    png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True, clip=clip).tobytes("png")
    doc.close()
    return png, len(marked)

"""Разбор PDF и DOCX на фрагменты с сохранением номера пункта и страницы.

Документ читается построчно. Строка, начинающаяся с номера пункта («4.2»,
«4.2.1», «12.»), открывает новый пункт; короткая строка с номером раздела
(«4 Порядок проведения») или римским номером («II. Требования…») становится
заголовком. Пункты затем собираются во фрагменты удобного для поиска размера:
мелкие соседние пункты объединяются, длинные режутся с перекрытием, но номер
пункта и страницы всегда остаются при фрагменте — они и есть ссылка в ответе.
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf
import yaml
from docx import Document as DocxDocument

from .text import join_hyphenated, normalize

CLAUSE_RE = re.compile(r"^(?:п\.\s*)?([1-9]\d{0,2}(?:\.\d{1,3}){0,4})(\.?)\s+(?=[А-ЯЁA-Z«\"(])")
ROMAN_RE = re.compile(r"^([IVXL]{1,6})\.\s+[А-ЯЁA-Z]")
ANNEX_RE = re.compile(r"^(Приложение\s+(?:N|№)?\s*[\dА-Я]+)", re.IGNORECASE)

TARGET, MAX_LEN, OVERLAP = 900, 1400, 150


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    doc_kind: str
    access: str
    clause: str | None
    heading: str | None
    page_start: int | None
    page_end: int | None
    text: str

    @property
    def search_text(self) -> str:
        """Текст для индексации: заголовок документа и раздела помогают поиску."""
        head = f"{self.doc_title}. {self.heading or ''}."
        return f"{head}\n{self.text}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Unit:
    clause: str | None
    heading: str | None
    page_start: int | None
    page_end: int | None
    lines: list

    @property
    def text(self) -> str:
        return " ".join(self.lines).strip()


# ---------- чтение файлов ----------

def _pdf_pages(path: Path) -> list[tuple[int, list[str]]]:
    doc = pymupdf.open(path)
    pages = []
    for i, page in enumerate(doc):
        # естественный порядок блоков: при sort=True колонки двухколоночной вёрстки перемешиваются
        raw = normalize(page.get_text("text"))
        lines = [l.strip() for l in raw.split("\n")]
        pages.append((i + 1, [l for l in lines if l]))
    # повторяющиеся колонтитулы и номера страниц
    if len(pages) >= 4:
        freq = Counter(l for _, lines in pages for l in set(lines) if len(l) < 80)
        boiler = {l for l, c in freq.items() if c >= max(3, 0.3 * len(pages))}
    else:
        boiler = set()
    cleaned = []
    for no, lines in pages:
        lines = [l for l in lines if l not in boiler and not re.fullmatch(r"\d{1,4}|-\s*\d{1,4}\s*-", l)]
        cleaned.append((no, join_hyphenated(lines)))
    return cleaned


def _docx_pages(path: Path) -> list[tuple[int | None, list[str]]]:
    doc = DocxDocument(path)
    lines = []
    for p in doc.paragraphs:
        text = normalize(p.text).strip()
        if text:
            lines.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [normalize(c.text).strip() for c in row.cells]
            row_text = " | ".join(dict.fromkeys(c for c in cells if c))
            if row_text:
                lines.append(row_text)
    return [(None, lines)]


# ---------- структура: пункты и заголовки ----------

def _is_heading(line: str, number: str | None, dotted: bool) -> bool:
    if ROMAN_RE.match(line) or ANNEX_RE.match(line):
        return True
    if number and "." not in number:
        # «4 Порядок проведения» — заголовок раздела ГОСТа: номер без точки и короткая строка.
        # «99. Перед хранением…» — пункт приказа, даже если строка оборвана переносом.
        return not dotted and len(line) < 90 and not line.rstrip().endswith((".", ";", ":"))
    letters = [c for c in line if c.isalpha()]
    return 8 < len(line) < 90 and len(letters) > 5 and all(c.isupper() for c in letters)


def _split_inline_clauses(lines: list[str], last_top):
    """В PDF приказов пункт может начинаться в середине строки:
    «…ремонт инструмента. 117. Работать с …». Строка разрезается, если внутри неё
    стоит номер следующего по порядку пункта. Генератор: номер последнего пункта
    обновляется по мере разбора, поэтому проверка идёт по актуальному значению."""
    for line in lines:
        while True:
            n = last_top() + 1
            m = re.search(rf"(?<=[.;:)»\"])\s+(?:{n}|{n + 1}|{n + 2})\.\s+(?=[А-ЯЁ])", line)
            if not m or m.start() < 3:
                yield line
                break
            yield line[:m.start()].strip()
            line = line[m.start():].strip()


def _units(pages) -> list[_Unit]:
    units: list[_Unit] = []
    heading = None
    last_top = 0
    current = _Unit(None, None, None, None, [])

    def flush():
        nonlocal current
        if current.lines:
            units.append(current)
        current = _Unit(current.clause, heading, None, None, [])

    for page_no, lines in pages:
        for line in _split_inline_clauses(lines, lambda: last_top):
            m = CLAUSE_RE.match(line)
            number = m.group(1) if m else None
            dotted = bool(m and m.group(2))
            if number and "." not in number:
                # одиночные номера принимаем только по порядку (допуская пропуск
                # до двух нераспознанных), чтобы «220 В» в тексте не стал пунктом 220
                value = int(number)
                if not (value == 1 or last_top < value <= last_top + 3):
                    number = None
            if _is_heading(line, number, dotted):
                flush()
                heading = line[:120]
                if number and "." not in number:
                    last_top = int(number)
                current = _Unit(number, heading, page_no, page_no, [])
                continue
            if number:
                flush()
                if "." not in number:
                    last_top = int(number)
                current = _Unit(number, heading, page_no, page_no, [line])
                continue
            if current.page_start is None:
                current.page_start = page_no
                current.heading = heading
            current.page_end = page_no
            current.lines.append(line)
    flush()
    return [u for u in units if u.text]


def _split_long(text: str) -> list[str]:
    parts, start = [], 0
    while start < len(text):
        end = min(start + TARGET, len(text))
        if end < len(text):
            cut = text.rfind(". ", start + TARGET // 2, end)
            end = cut + 1 if cut > 0 else end
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return parts


def _chunks(units: list[_Unit], meta: dict) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[_Unit] = []

    def emit(group: list[_Unit]):
        text = "\n".join(u.text for u in group)
        clauses = [u.clause for u in group if u.clause]
        if not clauses:
            clause = None
        elif clauses[0] == clauses[-1]:
            clause = clauses[0]
        else:
            clause = f"{clauses[0]}–{clauses[-1]}"
        pages = [p for u in group for p in (u.page_start, u.page_end) if p]
        base = dict(doc_id=meta["id"], doc_title=meta["title"], doc_kind=meta["kind"], access=meta["access"],
                    clause=clause, heading=group[0].heading,
                    page_start=min(pages) if pages else None, page_end=max(pages) if pages else None)
        for piece in (_split_long(text) if len(text) > MAX_LEN else [text]):
            chunks.append(Chunk(id=f"{meta['id']}#{len(chunks)}", text=piece, **base))

    # Пункты одного раздела объединяются, пока фрагмент не превысит целевой размер:
    # короткий раздел («2 Классификация отказов» из четырёх пунктов) остаётся целым.
    for unit in units:
        same_section = buf and buf[-1].heading == unit.heading
        size = sum(len(u.text) for u in buf)
        if buf and (not same_section or size + len(unit.text) > TARGET):
            emit(buf)
            buf = []
        buf.append(unit)
    if buf:
        emit(buf)
    return chunks


# ---------- каталог документов ----------

def load_catalog(raw_dir: Path) -> dict[str, dict]:
    """Названия и группы доступа из data/sources.yaml и data/raw/*/catalog.yaml."""
    catalog = {}
    for path in [raw_dir.parent / "sources.yaml", *raw_dir.glob("*/catalog.yaml")]:
        if path.exists():
            for d in yaml.safe_load(path.read_text(encoding="utf-8"))["documents"]:
                catalog[d["id"]] = d
    return catalog


def parse_document(path: Path, catalog: dict) -> list[Chunk]:
    info = catalog.get(path.stem, {})
    meta = {
        "id": path.stem,
        "title": info.get("title", path.stem),
        "kind": info.get("kind", "Документ"),
        "access": info.get("access", "all"),
    }
    pages = _pdf_pages(path) if path.suffix.lower() == ".pdf" else _docx_pages(path)
    return _chunks(_units(pages), meta)


def parse_corpus(raw_dir: Path) -> list[Chunk]:
    catalog = load_catalog(raw_dir)
    chunks = []
    for path in sorted(raw_dir.rglob("*")):
        if path.suffix.lower() in (".pdf", ".docx") and not path.name.startswith("~$"):
            chunks.extend(parse_document(path, catalog))
    return chunks

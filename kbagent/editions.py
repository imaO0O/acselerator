"""Что изменилось в новой редакции документа — по пунктам, со ссылками.

Редакции часто перестроены: пункт «2.1» старой редакции стал «3.1» новой, добавились разделы.
Поэтому пункты сопоставляются не по номеру, а по сходству текста (общие основы слов), один к одному,
начиная с самых похожих пар. Внутри пары изменения показываются по словам: ~~было~~ **стало**.
Модель не участвует — сравнение точное и повторяемое.
"""
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .ingest import _docx_pages, _pdf_pages, _units, load_catalog
from .text import tokenize

SIMILAR = 0.3          # доля общих основ слов, при которой пункты — один и тот же, только изменённый


@dataclass
class Clause:
    number: str
    text: str
    page: int | None


@dataclass
class Change:
    kind: str                       # changed | added | removed
    new: Clause | None = None
    old: Clause | None = None
    diff: str = ""                  # текст с пометками ~~было~~ **стало**


@dataclass
class Comparison:
    old_id: str
    new_id: str
    old_title: str
    new_title: str
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0

    def count(self, kind: str) -> int:
        return sum(c.kind == kind for c in self.changes)


def _source(doc_id: str) -> Path | None:
    for p in settings.raw_dir.rglob(f"{doc_id}.*"):
        if p.suffix.lower() in (".pdf", ".docx"):
            return p
    return None


def clauses(doc_id: str) -> list[Clause]:
    """Пункты документа по порядку — так же, как их разбирает индексация."""
    path = _source(doc_id)
    if path is None:
        return []
    pages = _pdf_pages(path) if path.suffix.lower() == ".pdf" else _docx_pages(path)
    return [Clause(u.clause, u.text, u.page_start) for u in _units(pages) if u.clause]


def edition_pairs(catalog: dict | None = None) -> dict[str, str]:
    """Новая редакция → прежняя (по каталогу: у прежней указано, чем заменена)."""
    catalog = catalog if catalog is not None else load_catalog(settings.raw_dir)
    return {info["replaced_by"]: doc_id for doc_id, info in catalog.items() if info.get("replaced_by")}


def _similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _body(text: str) -> str:
    return re.sub(r"^\s*\d{1,3}(?:\.\d{1,3})*\.?\s+", "", text).strip()       # без номера пункта


def word_diff(old: str, new: str) -> str:
    """«При отказе ~~оборудования~~ **категории А** … по телефону ~~2200~~ **2211**»."""
    a, b = _body(old).split(), _body(new).split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
        if op in ("delete", "replace"):
            out.append(f"~~{' '.join(a[i1:i2])}~~")
        if op in ("insert", "replace"):
            out.append(f"**{' '.join(b[j1:j2])}**")
    return " ".join(out)


def compare(old_id: str, new_id: str, catalog: dict | None = None) -> Comparison:
    catalog = catalog if catalog is not None else load_catalog(settings.raw_dir)
    result = compare_clauses(clauses(old_id), clauses(new_id))
    result.old_id, result.new_id = old_id, new_id
    result.old_title = catalog.get(old_id, {}).get("title", old_id)
    result.new_title = catalog.get(new_id, {}).get("title", new_id)
    return result


def chunk_clauses(chunks) -> list[Clause]:
    """Пункты документа из фрагментов индекса (длинный пункт разбит на части — склеивается обратно): так можно
    сравнить файл, обновлённый на месте, — прежнего файла уже нет, а его фрагменты ещё в индексе."""
    out: list[Clause] = []
    for c in chunks:
        number = c.clause or ""
        if out and out[-1].number == number:
            out[-1].text += " " + c.text
        else:
            out.append(Clause(number, c.text, c.page_start))
    return [c for c in out if c.number]


def compare_clauses(old: list[Clause], new: list[Clause]) -> Comparison:
    words_old = [set(tokenize(_body(c.text))) for c in old]
    words_new = [set(tokenize(_body(c.text))) for c in new]
    pairs = sorted(((_similarity(wo, wn), i, j) for i, wo in enumerate(words_old) for j, wn in enumerate(words_new)),
                   reverse=True)
    matched_old, matched_new, match = set(), set(), {}
    for score, i, j in pairs:
        if score < SIMILAR:
            break
        if i not in matched_old and j not in matched_new:
            matched_old.add(i)
            matched_new.add(j)
            match[j] = i
    result = Comparison("", "", "", "")
    for j, c in enumerate(new):                    # порядок — как в новой редакции
        if j not in match:
            result.changes.append(Change("added", new=c))
        elif _body(old[match[j]].text).split() == _body(c.text).split():
            result.unchanged += 1
        else:
            result.changes.append(Change("changed", new=c, old=old[match[j]], diff=word_diff(old[match[j]].text, c.text)))
    result.changes += [Change("removed", old=c) for i, c in enumerate(old) if i not in matched_old]
    return result

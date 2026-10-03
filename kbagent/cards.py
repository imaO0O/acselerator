"""Карточки документов: название, вид, объём, область применения и разделы — дословно из текста.

Нужны для вопросов о самой базе знаний («какие документы есть про паяльные станции?») и
для пересказа документа («о чём ГОСТ Р 2.105-2019?»): обычный поиск находит отдельные пункты,
а не документ целиком, и модель пересказывала случайные фрагменты. Карточка собирается из
фрагментов индекса без языковой модели, поэтому в ней нет ничего придуманного.
"""
import re
from collections import OrderedDict

from .ingest import Chunk
from .store import Index

SCOPE_RE = re.compile(r"област\w* применени|назначени|общие положени|введени|общие сведени|описание", re.I)


def build_cards(index: Index) -> dict[str, dict]:
    docs: "OrderedDict[str, list[Chunk]]" = OrderedDict()
    for c in index.chunks:
        docs.setdefault(c.doc_id, []).append(c)
    cards = {}
    for doc_id, chunks in docs.items():
        first = chunks[0]
        headings = list(dict.fromkeys(
            h for h in (c.heading for c in chunks)
            if h and len(h) < 90 and re.match(r"^\d{1,2}\.?\s+[А-ЯЁA-Z][а-яёa-z]", h)))   # «4 ВВЕДЕН ВПЕРВЫЕ» — не раздел
        scope_chunk = next((c for c in chunks if c.heading and SCOPE_RE.search(c.heading)), None) or \
            next((c for c in chunks if c.clause and c.clause.split(".")[0] == "1"), None) or first
        pages = max((c.page_end or 0) for c in chunks)
        cards[doc_id] = {
            "doc_id": doc_id, "title": first.doc_title, "kind": first.doc_kind, "access": first.access,
            "pages": pages or None, "chunks": len(chunks), "sections": headings[:20],
            "scope": re.sub(r"\s+", " ", scope_chunk.text)[:700],
        }
    return cards


def card_text(card: dict) -> str:
    parts = [f"Документ: {card['title']}", f"Вид: {card['kind']}"]
    if card.get("pages"):
        parts.append(f"Объём: {card['pages']} стр.")
    parts.append(f"Начало документа (область применения): {card['scope']}")
    if card["sections"]:
        parts.append("Разделы: " + "; ".join(card["sections"]))
    return "\n".join(parts)


def card_chunk(card: dict) -> Chunk:
    """Карточка в виде фрагмента — чтобы ответ по ней проходил те же проверки ссылок."""
    return Chunk(id=f"{card['doc_id']}#card", doc_id=card["doc_id"], doc_title=card["title"],
                 doc_kind=card["kind"], access=card["access"], clause=None, heading="Карточка документа",
                 page_start=1 if card.get("pages") else None, page_end=1 if card.get("pages") else None,
                 text=card_text(card))

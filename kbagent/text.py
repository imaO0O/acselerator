"""Нормализация текста и токенизация для полнотекстового поиска."""
import re
from functools import lru_cache

import snowballstemmer

_ru = snowballstemmer.stemmer("russian")
_en = snowballstemmer.stemmer("english")

SOFT_HYPHEN = "­"

STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до его ее если есть
еще же за здесь и из или им их к как ко когда кто ли либо мне может мы на над надо наш не него нее нет ни них но
ну о об однако он она они оно от по под при с со так также такой там те тем то того тоже той только том ты у уже
хотя чего чей чем что чтобы чье чья эта эти это я какой какая какие каким каких который которая которые которых
нужно ли можно ли сколько
""".split())

TOKEN_RE = re.compile(r"[a-zа-яё]+|\d+(?:[.,]\d+)*", re.IGNORECASE)
MD_SPECIAL_RE = re.compile(r"([\\`*\[\]()#!|<>~])")      # «_» внутри слова разметку не включает
MD_ESCAPED_RE = re.compile(r"\\([\\`*\[\]()#!|<>~])")


def md_unescape(text: str) -> str:
    """Обратно к обычному тексту — для документа Word и журнала."""
    return MD_ESCAPED_RE.sub(r"\1", text)


def md_plain(text: str) -> str:
    """Текст пользователя (вопрос, имя файла) — для показа через Markdown как есть: без ссылок, картинок, жирного."""
    return MD_SPECIAL_RE.sub(r"\\\1", text)


def normalize(text: str) -> str:
    """Приводит извлечённый из PDF текст к виду, пригодному для поиска и показа."""
    # мягкий перенос в конце строки — разрыв слова, в середине строки — обычный дефис
    text = re.sub(SOFT_HYPHEN + r"\s*\n\s*", "", text)
    text = text.replace(SOFT_HYPHEN, "-")
    text = text.replace("‐", "-").replace("‑", "-")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = text.replace("\xa0", " ").replace(" ", " ").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def join_hyphenated(lines: list[str]) -> list[str]:
    """Склеивает слова, разорванные переносом в конце строки («опас-» + «ного»)."""
    out: list[str] = []
    for line in lines:
        if out and re.search(r"[а-яё]-$", out[-1]) and re.match(r"^[а-яё]", line):
            out[-1] = out[-1][:-1] + line
        else:
            out.append(line)
    return out


@lru_cache(maxsize=200_000)
def _stem(word: str) -> str:
    if re.match(r"[a-z]", word):
        return _en.stemWord(word)
    return _ru.stemWord(word)


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in TOKEN_RE.findall(text.lower().replace("ё", "е")):
        if raw in STOPWORDS:
            continue
        if raw[0].isdigit():
            tokens.append(raw.replace(",", "."))
        elif len(raw) > 1:
            tokens.append(_stem(raw))
    return tokens

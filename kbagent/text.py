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


# ---------- обозначения: модели, артикулы, децимальные номера, ТУ ----------
# Обычный разбор режет «АКИП-4122/10» на «акип», «4122», «10», а «АБВГ.468157.001-01» — на «абвг», «468157.001»,
# «01»: модели одной серии почти не различаются. Поэтому обозначение добавляется к словам поиска ещё и целиком,
# одним редким «словом» — при совпадении оно сильно поднимает нужный фрагмент. Похожие русские и латинские буквы
# (А/A, В/B, С/C…) уравниваются, разделители (пробел, точка, дефис, косая черта) не важны.
LOOKALIKE = str.maketrans("авекмнорстху", "abekmhopctxy")
DECIMAL_RE = re.compile(r"(?<![\w.])([a-zа-яё]{4})\s?[.\s]\s?(\d{6})\s?[.\s]\s?(\d{3})(?:\s?-\s?(\d{2,3}))?(?!\w)", re.I)
DECIMAL_DIGITS_RE = re.compile(r"(?<![\w.])(\d{6})\.(\d{3})(?:-(\d{2,3}))?(?![\w.]*\w)")
TU_RE = re.compile(r"(?<!\w)ту\s*((?:\d{2}\.\d{2}\.\d{2}|\d{4})\s*-\s*\d{3}\s*-\s*\d{8}\s*-\s*\d{2,4})", re.I)
CODE_RE = re.compile(r"(?<![\w./-])[a-zа-яё0-9]+(?:[./+-][a-zа-яё0-9]+)*\+?(?![\w./-]*\w)", re.I)
PREFIX_RE = re.compile(r"([a-zа-яё]+)-?(\d.*)", re.I)                 # «АКИП-4115/2В» → «4115/2В»
SERIES_RE = re.compile(r"(?<![\w-])([A-ZА-ЯЁ]{2,6})\s+(\d[\w./+-]*)")  # «АКИП 4115/2В» — как «АКИП-4115/2В»
QUANTITY_RE = re.compile(r"\d+(?:[.,]\d+)?[a-zа-яё°]{1,4}", re.I)     # «150МГц», «15А» — величины, не обозначения


def _compact(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я+]", "", s.lower().replace("ё", "е")).translate(LOOKALIKE)


def _is_code(c: str, raw: str = "") -> bool:
    """Буквы и цифры («MAS830L») или цифры через косую черту — исполнение модели («4122/10» из «АКИП-4122/10»)."""
    return len(c) >= 4 and bool(re.search(r"\d", c)) and (bool(re.search(r"[a-zа-я]", c)) or "/" in raw)


def designation_tokens(text: str) -> list[str]:
    """Обозначения из текста целиком: «code:akип41152b» и «code:41152b» («АКИП-4115/2В», «акип 4115/2в»,
    «4115/2В» совпадают), «dn:абвг468157001» и «dn:468157001» (децимальный номер, в том числе без букв),
    «tu:4221001123456782015» (ТУ)."""
    out = []
    for m in DECIMAL_RE.finditer(text):
        letters, a, b, ex = m.group(1).lower().replace("ё", "е").translate(LOOKALIKE), m.group(2), m.group(3), m.group(4)
        out += [f"dn:{letters}{a}{b}", f"dn:{a}{b}"] + ([f"dn:{letters}{a}{b}{ex}", f"dn:{a}{b}{ex}"] if ex else [])
    for m in DECIMAL_DIGITS_RE.finditer(text):
        out += [f"dn:{m.group(1)}{m.group(2)}"] + ([f"dn:{m.group(1)}{m.group(2)}{m.group(3)}"] if m.group(3) else [])
    for m in TU_RE.finditer(text):
        out.append("tu:" + re.sub(r"\D", "", m.group(1)))
    for m in CODE_RE.finditer(text):
        raw = m.group(0)
        if QUANTITY_RE.fullmatch(raw):
            continue
        if _is_code(c := _compact(raw), raw):
            out.append(f"code:{c}")
        if (p := PREFIX_RE.fullmatch(raw)) and _is_code(c := _compact(p.group(2)), p.group(2)):
            out.append(f"code:{c}")
    for m in SERIES_RE.finditer(text):
        if m.group(1).lower() not in STOPWORDS and _is_code(c := _compact(m.group(1) + m.group(2))):
            out.append(f"code:{c}")
    return list(dict.fromkeys(out))


def word_tokens(text: str) -> list[str]:
    """Слова для поиска по словам (BM25): основы слов и числа + обозначения целиком."""
    return tokenize(text) + designation_tokens(text)

"""Нормоконтроль организационно-распорядительного документа (приказ, распоряжение, письмо, служебная
записка) в формате DOCX по ГОСТ Р 7.0.97-2016 — программно, по параметрам документа, без языковой модели.

Каждое правило ссылается на пункт стандарта, который лежит в базе знаний, — замечание открывается
вместе с текстом пункта. Значения взяты из стандарта: поля не менее 20/10/20/20 мм (п. 6.1), шрифт
№ 12–14 (п. 3.3), абзацный отступ 1,25 см и интервал 1–1,5 (п. 3.4), выравнивание по ширине (п. 3.5),
нумерация страниц посередине верхнего поля (п. 3.2), заголовок к тексту с предлогом «О» (п. 5.17),
дата 05.06.2016 или «5 июня 2016 г.» (п. 5.10). Реквизиты и содержание по-прежнему проверяет человек.
"""
import io
import re
from dataclasses import dataclass, field

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Length

GOST_ID = "gost_r_7_0_97_2016"
MARGINS = {"left_margin": ("левое", 20), "right_margin": ("правое", 10),
           "top_margin": ("верхнее", 20), "bottom_margin": ("нижнее", 20)}
DOC_TYPE_RE = re.compile(r"^\s*(?:ПРИКАЗ|РАСПОРЯЖЕНИЕ|ПОСТАНОВЛЕНИЕ|РЕШЕНИЕ|ПОЛОЖЕНИЕ|ИНСТРУКЦИЯ|АКТ|"
                         r"СЛУЖЕБНАЯ ЗАПИСКА|ДОКЛАДНАЯ ЗАПИСКА|ПРОТОКОЛ)\b")
SERVICE_LINE_RE = re.compile(r"^\s*(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[а-я]+\s+\d{4}|г\.\s|№|"
                             r"\d{4}[./-]\d{1,2}[./-]\d{1,2})", re.I)
BAD_DATE_RE = re.compile(r"(?<![\d./-])(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2}|"
                         r"\d{1,2}\.\d{1,2}\.\d{2})(?![\d./-])")


@dataclass
class Issue:
    level: str          # нарушение | рекомендация
    text: str
    clause: str         # пункт ГОСТ Р 7.0.97


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    paragraphs: int = 0


def _style_chain(style):
    while style is not None:
        yield style
        style = style.base_style


def _doc_default(doc, tag: str, attr: str):
    """Значение из умолчаний документа (docDefaults): размер шрифта, интервал."""
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    if defaults is None:
        return None
    node = defaults.find(f".//{qn(tag)}")
    return None if node is None else node.get(qn(attr))


def _pf(paragraph, name: str):
    value = getattr(paragraph.paragraph_format, name)
    for style in _style_chain(paragraph.style):
        if value is not None:
            break
        value = getattr(style.paragraph_format, name)
    return value


def _font_sizes(doc, paragraph) -> set[float]:
    sizes = set()
    for run in paragraph.runs:
        if not run.text.strip():
            continue
        size = run.font.size
        for style in _style_chain(paragraph.style):
            if size is not None:
                break
            size = style.font.size
        if size is None:
            half_points = _doc_default(doc, "w:sz", "w:val")
            size = Length(int(half_points) * 6350) if half_points else None
        if size is not None:
            sizes.add(round(size.pt, 1))
    return sizes


def _line_spacing(doc, paragraph):
    value = _pf(paragraph, "line_spacing")
    if value is None:
        line = _doc_default(doc, "w:spacing", "w:line")
        rule = _doc_default(doc, "w:spacing", "w:lineRule")
        if line and rule in (None, "auto"):
            value = int(line) / 240
    return value if isinstance(value, float) else None     # точное значение в пунктах не сравниваем


def _is_body(paragraph) -> bool:
    """Абзац основного текста: длинный, не заголовок, не элемент списка, не центрированный реквизит."""
    text = paragraph.text.strip()
    style = (paragraph.style.name or "").lower()
    numbered = paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None
    return (len(text) >= 100 and not numbered and not style.startswith(("heading", "заголовок", "title", "list"))
            and _pf(paragraph, "alignment") not in (WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.RIGHT))


def _quote(paragraph) -> str:
    text = re.sub(r"\s+", " ", paragraph.text.strip())
    return f"«{text[:60]}…»" if len(text) > 60 else f"«{text}»"


def _has_page_field(part) -> bool:
    xml = part._element.xml if part is not None else ""
    return bool(re.search(r"instr(?:Text)?[^>]*>\s*PAGE\b|w:instr=\"\s*PAGE\b", xml))


def check_docx(data: bytes) -> Report:
    doc = Document(io.BytesIO(data))
    report = Report()
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]
    report.paragraphs = len(paragraphs)

    # п. 6.1 — формат и поля
    section = doc.sections[0]
    width, height = round(section.page_width.mm), round(section.page_height.mm)
    if (min(width, height), max(width, height)) in [(210, 297), (148, 210)] or \
            any(abs(min(width, height) - a) <= 2 and abs(max(width, height) - b) <= 2 for a, b in [(210, 297), (148, 210)]):
        report.passed.append("формат листа")
    else:
        report.issues.append(Issue("нарушение", f"Формат листа {width}×{height} мм — нужен А4 (210×297) или А5", "6.1"))
    bad_margins = []
    for attr, (name, need) in MARGINS.items():
        value = getattr(section, attr)
        if value is not None and value.mm < need - 0.5:
            bad_margins.append(f"{name} поле — {value.mm:.0f} мм (нужно не менее {need})")
    if bad_margins:
        report.issues.append(Issue("нарушение", "; ".join(bad_margins).capitalize(), "6.1"))
    else:
        report.passed.append("поля")

    body = [p for p in paragraphs if _is_body(p)]

    # п. 3.3 — размер шрифта
    small = [p for p in body if any(s < 12 or s > 14 for s in _font_sizes(doc, p))]
    if small:
        sizes = sorted({s for p in small for s in _font_sizes(doc, p) if s < 12 or s > 14})
        report.issues.append(Issue("рекомендация", f"Размер шрифта {', '.join(f'{s:g}' for s in sizes)} пт в абзацах: "
                                   f"{len(small)} (например, {_quote(small[0])}) — рекомендуется № 12, 13 или 14", "3.3"))
    elif body:
        report.passed.append("размер шрифта")

    # п. 3.4 — абзацный отступ и интервал
    indent_bad = []
    for p in body:
        indent = _pf(p, "first_line_indent")
        cm = indent.cm if indent is not None else 0.0
        if abs(cm - 1.25) > 0.06:
            indent_bad.append((p, cm))
    if indent_bad:
        values = sorted({f"{cm:.2f}".rstrip("0").rstrip(".").replace(".", ",") for _, cm in indent_bad})
        report.issues.append(Issue("нарушение", f"Абзацный отступ {', '.join(values)} см в абзацах: {len(indent_bad)} "
                                   f"(например, {_quote(indent_bad[0][0])}) — нужен 1,25 см", "3.4"))
    elif body:
        report.passed.append("абзацный отступ")
    spacing_bad = [(p, s) for p in body if (s := _line_spacing(doc, p)) is not None and not 0.99 <= s <= 1.51]
    if spacing_bad:
        values = sorted({f"{s:.2f}".rstrip("0").rstrip(".").replace(".", ",") for _, s in spacing_bad})
        report.issues.append(Issue("нарушение", f"Межстрочный интервал {', '.join(values)} в абзацах: {len(spacing_bad)} "
                                   "— текст печатается через 1–1,5 интервала", "3.4"))
    elif body:
        report.passed.append("межстрочный интервал")
    spaces = [p for p in paragraphs if re.search(r"\S {2,}\S", p.text)]
    if spaces:
        report.issues.append(Issue("нарушение", f"Двойные пробелы между словами в абзацах: {len(spaces)} "
                                   f"(например, {_quote(spaces[0])}) — между словами один пробел", "3.4"))
    else:
        report.passed.append("пробелы между словами")

    # п. 3.5 — выравнивание по ширине
    not_justified = [p for p in body if _pf(p, "alignment") != WD_ALIGN_PARAGRAPH.JUSTIFY]
    if not_justified:
        report.issues.append(Issue("нарушение", f"Текст не выровнен по ширине в абзацах: {len(not_justified)} "
                                   f"(например, {_quote(not_justified[0])})", "3.5"))
    elif body:
        report.passed.append("выравнивание по ширине")

    # п. 3.2 — нумерация страниц (документ больше страницы)
    text_len = sum(len(p.text) for p in paragraphs)
    in_header = any(_has_page_field(s.header) for s in doc.sections)
    in_footer = any(_has_page_field(s.footer) for s in doc.sections)
    if text_len > 2500 and not in_header:
        report.issues.append(Issue("нарушение", "Номер страницы — внизу листа; нужно посередине верхнего поля"
                                   if in_footer else "Страницы не пронумерованы — со второй страницы номер ставится "
                                                     "посередине верхнего поля", "3.2"))
    elif text_len > 2500:
        report.passed.append("нумерация страниц")

    # п. 5.17 — заголовок к тексту с предлогом «О»
    for i, p in enumerate(paragraphs[:15]):
        if DOC_TYPE_RE.match(p.text):
            title = next((q for q in paragraphs[i + 1:i + 6] if not SERVICE_LINE_RE.match(q.text)
                          and not DOC_TYPE_RE.match(q.text) and len(q.text.strip()) <= 200), None)
            if title is not None and not re.match(r"^\s*(?:О|Об|Обо)\s", title.text):
                report.issues.append(Issue("нарушение", f"Заголовок к тексту {_quote(title)} — формулируется с предлогом "
                                           "«О» («Об») и отвечает на вопрос «о чём?»", "5.17"))
            elif title is not None:
                report.passed.append("заголовок к тексту")
            break

    # п. 5.10 — дата
    dates = sorted({m.group(0) for p in paragraphs for m in BAD_DATE_RE.finditer(p.text)})
    if dates:
        report.issues.append(Issue("нарушение", f"Дата в виде {', '.join(dates[:3])} — записывается как 05.06.2016 "
                                   "или «5 июня 2016 г.»", "5.10"))
    else:
        report.passed.append("формат дат")
    return report

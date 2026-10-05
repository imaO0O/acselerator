"""Ответ агента — в документ Word: вопрос, ответ, записи учёта, источники с пунктами и страницами.
Чтобы ответ можно было вложить в служебную записку или отправить коллеге без чата."""
import io
import re

from docx import Document
from docx.shared import Pt, RGBColor

from .database import display

INLINE_RE = re.compile(r"(\*\*.+?\*\*|~~.+?~~)")
GRAY = RGBColor(0x5C, 0x54, 0x70)


def _runs(paragraph, text: str) -> None:
    """**жирный**, ~~зачёркнутый~~ (сравнение редакций), остальное — обычный текст."""
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("~~") and part.endswith("~~"):
            paragraph.add_run(part[2:-2]).font.strike = True
        else:
            paragraph.add_run(part)


def _text(doc, text: str) -> None:
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if re.match(r"^\s*[-*•]\s+", line):
            _runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            _runs(doc.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        else:
            _runs(doc.add_paragraph(), line)


def answer_docx(item: dict, app_name: str, user: str | None = None) -> bytes:
    """item — элемент диалога чата: question, text, sources, columns/labels/rows, at, route, status."""
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    doc.add_heading("Вопрос", level=2)
    doc.add_paragraph(item["question"])
    doc.add_heading("Ответ", level=2)
    _text(doc, item["text"].replace("[БД]", "[учётная база]"))

    if item.get("rows"):
        labels = item.get("labels") or item.get("columns") or []
        doc.add_heading("Записи учётной базы", level=2)
        table = doc.add_table(rows=1, cols=len(labels))
        table.style = "Table Grid"
        for cell, label in zip(table.rows[0].cells, labels):
            cell.text = str(label)[:1].upper() + str(label)[1:]
            cell.paragraphs[0].runs[0].bold = True
        for row in item["rows"][:50]:
            for cell, value in zip(table.add_row().cells, row):
                cell.text = "" if value is None else display(value)

    cited = [s for s in item.get("sources", []) if s.get("cited")]
    if cited:
        doc.add_heading("Источники", level=2)
        for s in cited:
            doc.add_paragraph(f"[{s['n']}] {s['label']}")

    note = doc.add_paragraph()
    run = note.add_run(f"{app_name}. Сформировано {item.get('at', '')}" + (f", пользователь {user}" if user else "")
                       + ". Ответ составлен только по документам и учётной базе предприятия; перед применением "
                         "сверьте с источником.")
    run.italic, run.font.size, run.font.color.rgb = True, Pt(9), GRAY
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

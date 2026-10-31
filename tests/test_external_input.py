"""Входные данные от внешнего отправителя (почта, API, вложения): без фишинговых ссылок в интерфейсе и без
разбора без предела — «зип-бомба» в Word, скан на сотни страниц, вложение на сотни мегабайт."""
import io
import zipfile
from email.message import EmailMessage

import pytest

from kbagent import mailbox, tkp
from kbagent.text import md_external


def test_links_from_email_are_shown_as_plain_text():
    shown = md_external("Срочно: прайс по ссылке http://phish.example/x, www.evil.ru и [клик](https://a.b)")
    assert "http://" not in shown and "https://" not in shown and "www." not in shown
    assert "\\[клик\\]\\(" in shown                                    # разметка ссылки экранирована
    assert md_external("ООО «Ромашка»") == "ООО «Ромашка»"


def test_docx_bomb_is_not_unpacked():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document>" + " " * (tkp.LETTER_UNZIPPED + 1) + "</w:document>")
    assert len(buf.getvalue()) < 1_000_000                            # сжато — меньше мегабайта
    with pytest.raises(ValueError, match="распаковывается"):
        tkp.letter_text("Zapros.docx", buf.getvalue())


def test_only_first_pages_of_pdf_letter_are_read():
    import pymupdf
    doc = pymupdf.open()
    for n in range(1, 31):
        doc.new_page().insert_text((72, 72), f"Page {n}: DT9205A 2 pcs")         # встроенный шрифт — латиница
    text = tkp.letter_text("Zapros.pdf", doc.tobytes())
    assert "Page 10:" in text and "Page 11:" not in text


def test_huge_attachment_is_marked_not_parsed(monkeypatch):
    monkeypatch.setattr(mailbox, "MAX_ATTACHMENT", 1000)
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "client@zavod.ru", "Запрос КП"
    msg.set_content("Просим КП на 2 мультиметра для тока до 10 А.")
    msg.add_attachment(b"x" * 5000, maintype="application", subtype="pdf", filename="Skan.pdf")
    parsed = mailbox.parse_message(msg.as_bytes())
    assert parsed["attachments"] == ["Skan.pdf (больше 0 МБ — не разбиралось)"]
    assert "мультиметра" in parsed["letter"]

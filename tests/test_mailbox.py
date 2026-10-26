"""Почтовый ящик отдела → «Входящие запросы»: только чтение, письма и вложения, повторы, смена нумерации."""
import io
from email.message import EmailMessage

import pytest
from docx import Document

from kbagent import experts, mailbox
from kbagent.config import settings


def message(subject: str, body: str, html: bool = False, attachment: tuple[str, bytes] | None = None,
            msg_id: str = "") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Пётр Петров <petrov@zavod.ru>", "sales@demo.ru", subject
    m["Date"], m["Message-ID"] = "Mon, 26 Oct 2026 09:15:00 +0300", msg_id or f"<{abs(hash(subject + body))}@zavod.ru>"
    if html:
        m.set_content(body, subtype="html")
    else:
        m.set_content(body)
    if attachment:
        m.add_attachment(attachment[1], maintype="application", subtype="octet-stream", filename=attachment[0])
    return m.as_bytes()


def docx_bytes(text: str) -> bytes:
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class FakeIMAP:
    """Сервер с письмами {uid: bytes}; запоминает, как с ним работали."""
    def __init__(self, mails: dict[int, bytes], validity: str = "777"):
        self.mails, self.calls, self.untagged_responses = mails, [], {"UIDVALIDITY": [validity.encode()]}

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [" ".join(str(u) for u in sorted(self.mails)).encode()]
        uid = int(args[0])
        return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(self.mails[uid])}}}".encode(), self.mails[uid]), b")"]

    def logout(self):
        self.calls.append(("logout",))


@pytest.fixture
def box(tmp_path, monkeypatch, tmp_settings):
    d = tmp_path / "mail"
    monkeypatch.setattr(mailbox, "DIR", d)
    monkeypatch.setattr(mailbox, "INBOX", d / "inbox.json")
    monkeypatch.setattr(mailbox, "STATE", d / "state.json")
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    monkeypatch.setattr(settings, "mail_host", "imap.demo.ru")
    monkeypatch.setattr(settings, "mail_user", "sales@demo.ru")
    return mailbox


def test_requests_from_mail_body_attachment_and_html(box):
    server = FakeIMAP({
        1: message("Запрос КП", "Добрый день! ООО «Пример» просит КП: 2 осциллографа от 150 МГц на 4 канала."),
        2: message("Заявка", "Добрый день, заявка во вложении.",
                   attachment=("Zayavka.docx", docx_bytes("Нужны 3 мультиметра на ток до 15 А, подготовьте предложение"))),
        3: message("Приглашение на выставку", "Приглашаем вас на выставку «Электроника-2026»."),
        4: message("КП", "<p>Просим КП:</p><table><tr><td>Осциллограф</td><td>полоса от 200 МГц, 4 канала</td>"
                         "<td>1</td></tr></table>", html=True),
    })
    rep = box.check(connect=lambda: server)
    assert rep == {"checked": 4, "requests": 3, "skipped": 1, "error": ""}
    assert ("select", "INBOX", True) in server.calls                              # только чтение
    assert all(c[2] == "(BODY.PEEK[])" for c in server.calls if c[0] == "fetch")  # не помечать прочитанными
    inbox = box.load()
    assert [m["subject"] for m in inbox] == ["Запрос КП", "Заявка", "КП"]
    assert inbox[0]["positions"] == ["Осциллограф × 2"] and inbox[0]["sender"] == "petrov@zavod.ru"
    assert inbox[1]["attachments"] == ["Zayavka.docx"] and inbox[1]["positions"] == ["Мультиметр × 3"]
    assert inbox[2]["positions"] == ["Осциллограф × 1"] and all(m["status"] == "новый" for m in inbox)

    again = box.check(connect=lambda: FakeIMAP(server.mails))
    assert again["checked"] == 0 and len(box.load()) == 3                        # новые — только по UID
    renumbered = box.check(connect=lambda: FakeIMAP(server.mails, validity="888"))
    assert renumbered["checked"] == 4 and renumbered["requests"] == 0 and len(box.load()) == 3   # повторы отсечены

    item = box.set_status(inbox[0]["id"], "открыт", "m", "Менеджер")
    assert item["status"] == "открыт" and item["history"][0]["by"] == "Менеджер"


def test_mail_errors_are_reported_not_raised(box, monkeypatch):
    def broken():
        raise OSError("нет связи с сервером")
    assert "не удалось подключиться" in box.check(connect=broken)["error"]
    monkeypatch.setattr(settings, "mail_host", "")
    assert "не настроен" in box.check()["error"]

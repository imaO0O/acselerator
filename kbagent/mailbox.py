"""Почтовый ящик отдела → «Входящие запросы» с готовым разбором ТКП.

Только чтение: ящик открывается в режиме readonly, письма забираются командой BODY.PEEK — не помечаются
прочитанными, ничего не удаляется, не перемещается и не отправляется. Новые письма — по UID (data/mail/state.json);
если сервер сменил нумерацию (UIDVALIDITY), ящик просматривается заново, а повторы отсекаются по Message-ID.

Текст запроса — тело письма и вложения (Word, PDF, текст, .eml). Разбор — тот же, что в чате: позиции, подбор по
справочнику, цены из прайса — программой, без языковой модели; противоречия источников уходят эксперту. Письма
без продукции из справочника во «Входящие» не попадают. Менеджер открывает запрос в чате одной кнопкой — дальше
выбор прибора, утверждение и реестр, как обычно.

Настройки: KB_MAIL_HOST, KB_MAIL_PORT (993), KB_MAIL_USER, KB_MAIL_FOLDER (INBOX), KB_MAIL_SSL (1), пароль —
KB_MAIL_PASSWORD или файл data/mail_password.txt (в git и резервную копию не попадает). Проверка —
python scripts/check_mail.py --watch 300 или кнопка на странице «Реестр ТКП».
"""
import email
import email.policy
import hashlib
import html
import imaplib
import json
import re
import time
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from . import tkp
from .actions import log_action
from .config import ROOT, settings

DIR = settings.raw_dir.parent / "mail"
INBOX = DIR / "inbox.json"
STATE = DIR / "state.json"
PASSWORD_FILE = ROOT / "data" / "mail_password.txt"
STATUSES = {"новый": "новый", "открыт": "открыт в чате", "не запрос": "не запрос клиента"}
MAX_LETTER = 6000


def configured() -> bool:
    return bool(settings.mail_host and settings.mail_user)


def _password() -> str:
    import os
    value = os.environ.get("KB_MAIL_PASSWORD")
    if value is None and PASSWORD_FILE.exists():
        value = PASSWORD_FILE.read_text(encoding="utf-8")
    return (value or "").strip()


def _connect():
    cls = imaplib.IMAP4_SSL if settings.mail_ssl else imaplib.IMAP4
    conn = cls(settings.mail_host, settings.mail_port)
    conn.login(settings.mail_user, _password())
    return conn


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, data) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load() -> list[dict]:
    return _read(INBOX, [])


def _plain(htm: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", htm)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"(?i)</td>", " | ", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def parse_message(raw: bytes) -> dict:
    """Письмо → отправитель, тема, дата, текст тела и текст вложений-писем (Word, PDF, текст, .eml)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = _plain(text)
    attached, names = [], []
    for part in msg.iter_attachments():
        name = part.get_filename() or ""
        if name.lower().rsplit(".", 1)[-1] in tkp.LETTER_TYPES:
            try:
                attached.append(tkp.letter_text(name, part.get_payload(decode=True) or b""))
                names.append(name)
            except Exception:  # noqa: BLE001 — повреждённое вложение не мешает разобрать тело письма
                continue
    try:
        received = parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d %H:%M") if msg["Date"] else ""
    except (TypeError, ValueError):
        received = ""
    letter = "\n\n".join(x.strip() for x in [text, *attached] if x and x.strip())
    return {"message_id": (msg["Message-ID"] or "").strip() or hashlib.sha1(raw).hexdigest(),
            "sender": parseaddr(msg["From"] or "")[1] or str(msg["From"] or ""), "sender_name":
            parseaddr(msg["From"] or "")[0], "subject": str(msg["Subject"] or "").strip(), "received": received,
            "letter": letter[:MAX_LETTER], "attachments": names}


def _uidvalidity(conn) -> str:
    value = (getattr(conn, "untagged_responses", {}) or {}).get("UIDVALIDITY")
    return (value[0].decode() if isinstance(value[0], bytes) else str(value[0])) if value else ""


def check(connect=None) -> dict:
    """Один проход по ящику: новые письма с запросом на продукцию — во «Входящие» с разбором ТКП."""
    report = {"checked": 0, "requests": 0, "skipped": 0, "error": ""}
    if not configured():
        report["error"] = "почтовый ящик не настроен (KB_MAIL_HOST, KB_MAIL_USER)"
        return report
    state = _read(STATE, {"uidvalidity": "", "last_uid": 0})
    inbox = load()
    known = {m["message_id"] for m in inbox} | set(state.get("seen", []))
    allowed = settings.roles.get(settings.mail_role, {"all"})
    try:
        conn = (connect or _connect)()
    except (OSError, imaplib.IMAP4.error) as e:
        report["error"] = f"не удалось подключиться к почте: {e}"
        return report
    try:
        typ, _ = conn.select(settings.mail_folder, readonly=True)
        if typ != "OK":
            report["error"] = f"папка «{settings.mail_folder}» недоступна"
            return report
        validity = _uidvalidity(conn)
        if validity != state.get("uidvalidity"):
            state.update(uidvalidity=validity, last_uid=0)      # нумерация сменилась — повторы отсечёт Message-ID
        typ, data = conn.uid("search", None, f"UID {state['last_uid'] + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > state["last_uid"])
        for uid in uids:
            typ, parts = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
            state["last_uid"] = uid
            report["checked"] += 1
            mail = parse_message(raw)
            if mail["message_id"] in known:
                continue
            known.add(mail["message_id"])
            request = tkp.parse(mail["letter"], allowed) if mail["letter"] else None
            if request is None or not tkp.has_offer(request) or not (
                    tkp.is_request(mail["letter"], request) or tkp.TKP_WORDS_RE.search(mail["subject"])):
                report["skipped"] += 1
                state.setdefault("seen", []).append(mail["message_id"])
                continue
            tkp.notify_experts(request, mail["letter"][:300], "почта", settings.mail_role)
            d = request.to_dict()
            positions = [f"{p['kind']} × {p['qty']}" for p in d["positions"]]
            inbox.append({**mail, "id": hashlib.sha1(mail["message_id"].encode()).hexdigest()[:10], "uid": uid,
                          "status": "новый", "created": time.strftime("%Y-%m-%d %H:%M"), "positions": positions,
                          "offered": sum(1 for p in d["positions"] if p["status"] in ("offer", "named")),
                          "history": []})
            report["requests"] += 1
            log_action("mail_request", "почта", mail["subject"][:120] or mail["sender"],
                       f"от {mail['sender']}; позиций: {len(positions)}")
        state["seen"] = state.get("seen", [])[-2000:]
        _write(STATE, state)
        _write(INBOX, inbox)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return report


def set_status(mail_id: str, status: str, login: str | None, name: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"статус — один из: {', '.join(STATUSES)}")
    inbox = load()
    item = next((m for m in inbox if m["id"] == mail_id), None)
    if item is None:
        raise ValueError("письма нет во «Входящих»")
    item["status"] = status
    item["history"].append({"status": status, "by": name, "at": time.strftime("%d.%m.%Y %H:%M")})
    _write(INBOX, inbox)
    log_action("mail_status", login, item["subject"][:120] or item["sender"], STATUSES[status])
    return item

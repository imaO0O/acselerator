"""Реестр ТКП: номер, статус и файлы каждого утверждённого предложения; бланк и условия компании.

Номер присваивается при утверждении — «ТКП-2026-0001», с начала года заново; под блокировкой, чтобы два
менеджера не получили один номер. Утверждённые Word и Excel хранятся в data/tkp/files/ — из реестра скачивается
ровно то, что ушло клиенту. Статус меняет менеджер: утверждён → отправлен клиенту → выигран / проигран / отменён;
каждая смена — в истории записи и в журнале действий. Сводка для руководителя: сколько ТКП, на какую сумму,
доля выигранных среди решённых.

Бланк компании (data/tkp/blank.docx) — шапка, реквизиты, логотип, колонтитулы; условия (data/tkp/settings.yaml) —
название, префикс номера, срок действия, оплата, поставка. Всё это данные заказчика: в git не попадают
и входят в резервную копию.
"""
import datetime as dt
import io
import json
import re
from pathlib import Path

import yaml

from . import prajs, tkp
from .actions import log_action
from .config import settings

DIR = settings.raw_dir.parent / "tkp"
SETTINGS = DIR / "settings.yaml"
BLANK = DIR / "blank.docx"
REGISTRY = DIR / "registry.json"
FILES = DIR / "files"
DEFAULTS = {"company": "", "prefix": "ТКП-", "validity_days": 30, "payment": "", "delivery": ""}
STATUSES = ["утверждён", "отправлен клиенту", "выигран", "проигран", "отменён"]
CLIENT_RE = re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ОАО|ФГУП|МУП|ГУП|НПП|НПО|НИИ|ИП)\s*(?:[«\"][^»\"\n]{2,80}[»\"]|"
                       r"[А-ЯЁ][\w-]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)?)")


def load_settings() -> dict:
    try:
        data = yaml.safe_load(SETTINGS.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save_settings(values: dict) -> dict:
    data = {**load_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    data["validity_days"] = int(data["validity_days"] or 0)
    DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return data


def save_blank(data: bytes) -> str:
    """Бланк компании: проверяется, что это Word; возвращает подсказку — есть ли в нём место {{ТКП}}."""
    from docx import Document
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 — не Word или повреждён
        raise ValueError("нужен документ Word (.docx)") from e
    DIR.mkdir(parents=True, exist_ok=True)
    BLANK.write_bytes(data)
    if any(tkp.PLACEHOLDER in p.text for p in doc.paragraphs):
        return "ТКП встанет на место метки {{ТКП}}."
    return "Метки {{ТКП}} в бланке нет — ТКП встанет после текста бланка."


def blank() -> Path | None:
    return BLANK if BLANK.exists() else None


def load() -> list[dict]:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(records: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(REGISTRY)


def client_of(letter: str) -> str:
    """Организация клиента из письма: «ООО «Пример»», «АО «Прибор»» — иначе пусто."""
    m = CLIENT_RE.search(letter or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def _file(number: str, ext: str) -> Path:
    return FILES / f"{re.sub(r'[^\w-]+', '_', number)}.{ext}"


def files(number: str) -> tuple[Path, Path]:
    return _file(number, "docx"), _file(number, "xlsx")


def approve(t: dict, text: str, login: str | None, name: str, role: str, question: str, app_name: str,
            now: dt.datetime | None = None) -> dict:
    """Утверждение ТКП: номер, Word и Excel на бланке компании с этим номером, запись в реестре."""
    from .store import _lock
    now = now or dt.datetime.now()
    conf = load_settings()
    terms = {"payment": conf["payment"], "delivery": conf["delivery"]}
    if conf["validity_days"]:
        terms["valid_until"] = (now.date() + dt.timedelta(days=int(conf["validity_days"]))).strftime("%d.%m.%Y")
    when = now.strftime("%d.%m.%Y %H:%M")
    priced = tkp._priced(t["positions"])
    total = sum(p["price"] * p["qty"] for p in priced) if priced else None
    with _lock(DIR):
        records = load()
        seq = 1 + max((r["seq"] for r in records if r["year"] == now.year), default=0)
        number = f"{conf['prefix']}{now.year}-{seq:04d}"
        FILES.mkdir(parents=True, exist_ok=True)
        docx_path, xlsx_path = files(number)
        docx_path.write_bytes(tkp.tkp_docx(text, t, name, when, app_name, number=number, terms=terms, blank=blank()))
        xlsx_path.write_bytes(tkp.tkp_xlsx(t, number, when[:10], terms, conf["company"]))
        record = {
            "number": number, "year": now.year, "seq": seq, "date": now.strftime("%Y-%m-%d %H:%M"),
            "client": client_of(t.get("letter", "") or question), "question": question[:300],
            "positions": len(t["positions"]), "offered": sum(1 for p in t["positions"]
                                                             if p["status"] in ("offer", "named")),
            "models": [p["model"] for p in t["positions"] if p["status"] in ("offer", "named")],
            "total": total, "vat": tkp._vat(total)[0] if total and settings.vat_rate else None,
            "valid_until": terms.get("valid_until", ""), "status": STATUSES[0], "login": login, "approved_by": name,
            "role": role, "history": [{"status": STATUSES[0], "by": name, "at": when, "comment": ""}]}
        records.append(record)
        _save(records)
    log_action("tkp_registered", login, number, f"позиций: {record['positions']}"
               + (f"; сумма: {prajs.money(total)}" if total else ""))
    return record


def set_status(number: str, status: str, login: str | None, name: str, comment: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"статус — один из: {', '.join(STATUSES)}")
    from .store import _lock
    with _lock(DIR):
        records = load()
        record = next((r for r in records if r["number"] == number), None)
        if record is None:
            raise ValueError(f"ТКП {number} нет в реестре")
        record["status"] = status
        record["history"].append({"status": status, "by": name, "at": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
                                  "comment": comment.strip()})
        _save(records)
    log_action("tkp_status", login, number, status + (f": {comment.strip()}" if comment.strip() else ""))
    return record


def summary(records: list[dict]) -> dict:
    """Для руководителя: всего, сумма, выигранные, доля выигранных среди решённых (выигран + проигран)."""
    won = [r for r in records if r["status"] == "выигран"]
    lost = [r for r in records if r["status"] == "проигран"]
    return {"count": len(records), "total": sum(r["total"] or 0 for r in records),
            "won": len(won), "won_total": sum(r["total"] or 0 for r in won),
            "open": sum(1 for r in records if r["status"] in ("утверждён", "отправлен клиенту")),
            "win_rate": round(100 * len(won) / (len(won) + len(lost))) if won or lost else None}


def export(records: list[dict]) -> bytes:
    """Реестр в Excel — для отчёта и для CRM."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр ТКП"
    ws.append(["Номер", "Дата", "Клиент", "Позиций", "Приборы", "Сумма, ₽", "НДС, ₽", "Действует до", "Статус",
               "Утвердил", "Запрос клиента", "История"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in records:
        ws.append([r["number"], r["date"], r["client"], r["positions"], ", ".join(r["models"]), r["total"], r["vat"],
                   r["valid_until"], r["status"], r["approved_by"], r["question"],
                   "; ".join(f"{h['at']} {h['status']} ({h['by']})" + (f": {h['comment']}" if h["comment"] else "")
                             for h in r["history"])])
    for col, width in zip("ABCDEFGHIJKL", (16, 16, 26, 9, 30, 14, 12, 13, 18, 20, 50, 60)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

"""REST API агента — для подключения корпоративного мессенджера, чат-бота, портала или 1С.

    python scripts/run_api.py                  # http://127.0.0.1:8000, документация — /docs

Доступ — по ключу в заголовке X-API-Key. Ключи выпускает администратор (scripts/make_api_key.py);
у каждого ключа своя роль, поэтому права на документы соблюдаются так же, как в чате.
В data/api_keys.yaml хранятся только хэши ключей. Каждый запрос пишется в общий журнал.
"""
import base64
import binascii
import hashlib
import hmac
import sys
import time
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import changes, demand, llm, tkp  # noqa: E402
from kbagent.actions import log_action  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.ingest import allowed_groups  # noqa: E402

KEYS_PATH = ROOT / "data" / "api_keys.yaml"

app = FastAPI(title="Агент по документам и учёту — API", version="1.0",
              description="Ответы по внутренним документам и учётной базе предприятия со ссылкой на источник.")
_agent: Agent | None = None


def agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def client(x_api_key: str | None = Header(None, description="Ключ доступа, выданный администратором")) -> dict:
    if not x_api_key:
        raise HTTPException(401, "Нужен ключ доступа в заголовке X-API-Key")
    keys = yaml.safe_load(KEYS_PATH.read_text(encoding="utf-8")).get("keys", []) if KEYS_PATH.exists() else []
    h = key_hash(x_api_key)
    for k in keys:
        if hmac.compare_digest(k["hash"], h):
            if k["role"] not in settings.roles:
                raise HTTPException(500, "У ключа указана неизвестная роль")
            return {**k, "clearance": int(k.get("clearance", 0))}
    log_action("api_denied", None, "", "неверный ключ")
    raise HTTPException(401, "Неверный ключ доступа")


class Turn(BaseModel):
    question: str = Field(..., max_length=1000)
    answer: str = Field("", max_length=8000)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000, examples=["Что делать при искрении оборудования?"])
    history: list[Turn] = Field(default_factory=list, max_length=50,
                                description="Предыдущие реплики диалога; учитываются последние три")
    source: str = Field("auto", pattern="^(auto|documents|database)$", description="Где искать ответ")


class Source(BaseModel):
    n: int
    label: str
    doc_id: str
    clause: str | None = None
    page: int | None = None


class AskResponse(BaseModel):
    status: str = Field(description="answer | no_data | model_refused | unverified | clarify | error")
    route: str = Field(description="documents | database | both")
    text: str
    understood_as: str | None = None
    sources: list[Source] = []
    table: dict | None = Field(None, description="Записи учётной базы: {columns, rows}")
    options: list[str] = Field(default_factory=list, description="Варианты уточнения для status=clarify")
    seconds: float


class LetterFile(BaseModel):
    name: str = Field(..., max_length=200, examples=["Запрос_клиента.docx"], description="имя файла: .docx, .pdf, .txt, .eml")
    content_base64: str = Field(..., max_length=14_000_000, description="содержимое файла в base64 (до 10 МБ)")


class TkpRequest(BaseModel):
    letter: str = Field("", max_length=20_000, description="текст письма клиента — или file",
                        examples=["Просим КП: 2 осциллографа от 150 МГц на 4 канала, мультиметр на ток до 15 А — 3 шт."])
    file: LetterFile | None = None
    choices: dict[int, str] = Field(default_factory=dict,
                                    description="выбор менеджера: {номер позиции: прибор из fit_models}")
    signer: str = Field("", max_length=200, description="подпись в письме клиенту")
    include_docx: bool = Field(True, description="вернуть ТКП в Word (base64)")


class Check(BaseModel):
    param: str
    need: str
    value: str
    ok: bool | None = Field(description="true — соответствует, false — нет, null — не проверить")
    source: int | None = Field(None, description="номер источника из sources")


class Position(BaseModel):
    n: int
    kind: str
    qty: int
    requirements: list[str]
    status: str = Field(description="offer | named | no_fit | expert | no_reqs | not_in_catalog")
    model: str | None = None
    minimal: str = Field("", description="минимально достаточный прибор")
    fit_models: list[str] = Field(default_factory=list, description="все подходящие — для выбора менеджером")
    cheapest: str = ""
    checks: list[Check] = []
    price: float | None = None
    sum: float | None = None
    lead: str = ""
    note: str = ""
    ask_client: str = ""


class TkpResponse(BaseModel):
    status: str = Field(description="answer — есть предложение | no_offer — продукции нет в справочнике | "
                                    "not_request — в письме нет продукции из справочника")
    positions: list[Position] = []
    total: float | None = Field(None, description="итог по прайсу")
    vat: float | None = Field(None, description="НДС по ставке KB_VAT_RATE")
    sources: list[Source] = []
    text: str = Field("", description="разбор, как в чате")
    letter_draft: str = Field("", description="черновик письма клиенту")
    docx_base64: str | None = None
    seconds: float


class Change(BaseModel):
    time: str
    kind: str = Field(description="edition — новая редакция, added, updated, removed, effective, expired")
    kind_label: str
    doc_id: str
    title: str
    old_title: str | None = Field(None, description="прежняя редакция")
    summary: str = Field("", description="итог сравнения: «пунктов изменено: 3, добавлено: 1»")


@app.get("/changes", response_model=list[Change])
def doc_changes(since: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$",
                                   description="с какого момента: 2026-10-28 или 2026-10-28 09:00"),
                who: dict = Depends(client)) -> list[Change]:
    """Изменения документов, доступных роли ключа, — чтобы бот мессенджера публиковал их в канале отдела."""
    stamp = (since + " 00:00:00")[:19] if len(since) == 10 else (since + ":00")[:19]
    events = changes.feed(f"api:{who['name']}", who["role"], since=stamp, clearance=who["clearance"])
    return [Change(time=e["time"], kind=e["kind"], kind_label=changes.KINDS[e["kind"]], doc_id=e["doc_id"],
                   title=e["title"], old_title=e.get("old_title"), summary=e.get("summary", "")) for e in events]


@app.get("/health")
def health() -> dict:
    state = llm.health()
    return {"ok": all(state.values()), "models": state}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, who: dict = Depends(client)) -> AskResponse:
    # длинные ответы из истории обрезаются: модели поиска принимают ограниченный объём текста
    history = [{"question": t.question[:500], "text": t.answer[:1500]} for t in req.history[-3:]]
    try:
        r = agent().ask(req.question, role=who["role"], route=req.source, history=history or None,
                        user=f"api:{who['name']}", clearance=who["clearance"])
    except Exception as e:      # noqa: BLE001 — сбой модели или поиска: понятный ответ вместо «500»
        log_action("api_error", f"api:{who['name']}", req.question[:120], type(e).__name__)
        raise HTTPException(503, "Сервис ответов временно недоступен, повторите запрос позже") from e
    sources = [Source(n=s["n"], label=s["label"], doc_id=s["doc_id"], clause=s.get("clause"), page=s.get("page"))
               for s in r.sources if s["cited"]]
    table = {"columns": r.labels or r.columns, "rows": r.rows} if r.rows else None
    return AskResponse(status=r.status, route=r.route, text=r.text, understood_as=r.asked, sources=sources,
                       table=table, options=r.options, seconds=r.seconds)


@app.post("/tkp", response_model=TkpResponse)
def make_tkp(req: TkpRequest, who: dict = Depends(client)) -> TkpResponse:
    """Письмо клиента → позиции, подбор по справочнику, цены из прайса, черновик письма и ТКП в Word.
    Программой, без языковой модели: работает и когда серверы моделей не запущены. Автоотправки нет —
    ТКП утверждает сотрудник; противоречия источников уходят в очередь эксперта, как из чата."""
    t0 = time.time()
    user = f"api:{who['name']}"
    letter = req.letter.strip()
    if req.file is not None:
        try:
            data = base64.b64decode(req.file.content_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(422, "content_base64 — не base64") from e
        if not req.file.name.lower().endswith(tuple(f".{x}" for x in tkp.LETTER_TYPES)):
            raise HTTPException(422, "Поддерживаются письма .docx, .pdf, .txt, .eml")
        try:
            letter = tkp.letter_text(req.file.name, data).strip()
        except Exception as e:  # noqa: BLE001 — повреждённый файл
            raise HTTPException(422, "Не удалось прочитать файл письма") from e
    if len(letter) < 5:
        raise HTTPException(422, "Нужен текст письма (letter) или файл (file)")
    t = tkp.parse(letter, allowed_groups(who["role"], who["clearance"]), chosen=req.choices or None)
    if t is None:
        log_action("api_tkp", user, letter[:120], "нет продукции из справочника")
        return TkpResponse(status="not_request", seconds=round(time.time() - t0, 2))
    tkp.notify_experts(t, letter[:300], user, who["role"])
    d = t.to_dict()
    priced = [p for p in d["positions"] if p["status"] in ("offer", "named") and p.get("price")]
    total = sum(p["price"] * p["qty"] for p in priced) if priced else None
    positions = [Position(
        n=p["n"], kind=p["kind"], qty=p["qty"], requirements=p["reqs"], status=p["status"], model=p["model"],
        minimal=p.get("minimal", ""), fit_models=p.get("fit_models", []), cheapest=p.get("cheapest", ""),
        checks=[Check(param=c["param"], need=c["need"], value=c["value"], ok=c["ok"], source=c["ref"])
                for c in p["checks"]],
        price=p.get("price"), sum=p["price"] * p["qty"] if p.get("price") else None, lead=p.get("lead", ""),
        note=p["note"], ask_client=p["ask_client"]) for p in d["positions"]]
    draft = tkp.draft_text(d, req.signer) if tkp.has_offer(t) else ""
    docx = None
    if draft and req.include_docx:
        docx = base64.b64encode(tkp.tkp_docx(draft, d, "не утверждён — черновик через API",
                                             time.strftime("%d.%m.%Y %H:%M"), "AI-агент")).decode()
    log_action("api_tkp", user, letter[:120], f"позиций: {len(positions)}; с ценой: {len(priced)}")
    if not req.choices:                       # повторный разбор с выбором приборов — тот же запрос клиента
        demand.record("письмо клиента (API)", demand.from_tkp(d), who["role"], user)
    return TkpResponse(
        status="answer" if tkp.has_offer(t) else "no_offer", positions=positions, total=total,
        vat=tkp._vat(total)[0] if total and settings.vat_rate else None,
        sources=[Source(n=s["n"], label=s["label"], doc_id=s["doc_id"], page=s.get("page")) for s in d["sources"]],
        text=tkp.answer_text(t), letter_draft=draft, docx_base64=docx, seconds=round(time.time() - t0, 2))

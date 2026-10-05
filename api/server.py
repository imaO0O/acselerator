"""REST API агента — для подключения корпоративного мессенджера, чат-бота, портала или 1С.

    python scripts/run_api.py                  # http://127.0.0.1:8000, документация — /docs

Доступ — по ключу в заголовке X-API-Key. Ключи выпускает администратор (scripts/make_api_key.py);
у каждого ключа своя роль, поэтому права на документы соблюдаются так же, как в чате.
В data/api_keys.yaml хранятся только хэши ключей. Каждый запрос пишется в общий журнал.
"""
import hashlib
import hmac
import sys
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import llm  # noqa: E402
from kbagent.actions import log_action  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402

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
            return k
    log_action("api_denied", None, "", "неверный ключ")
    raise HTTPException(401, "Неверный ключ доступа")


class Turn(BaseModel):
    question: str
    answer: str = ""


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000, examples=["Что делать при искрении оборудования?"])
    history: list[Turn] = Field(default_factory=list, description="Предыдущие реплики диалога (до трёх)")
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


@app.get("/health")
def health() -> dict:
    state = llm.health()
    return {"ok": all(state.values()), "models": state}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, who: dict = Depends(client)) -> AskResponse:
    history = [{"question": t.question, "text": t.answer} for t in req.history[-3:]]
    r = agent().ask(req.question, role=who["role"], route=req.source, history=history or None,
                    user=f"api:{who['name']}")
    sources = [Source(n=s["n"], label=s["label"], doc_id=s["doc_id"], clause=s.get("clause"), page=s.get("page"))
               for s in r.sources if s["cited"]]
    table = {"columns": r.labels or r.columns, "rows": r.rows} if r.rows else None
    return AskResponse(status=r.status, route=r.route, text=r.text, understood_as=r.asked, sources=sources,
                       table=table, options=r.options, seconds=r.seconds)

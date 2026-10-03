"""Агент: выбирает источник (документы или учётная база), отвечает и пишет журнал."""
import json
import time
from dataclasses import dataclass, field

from . import llm
from .answer import answer_from_documents, source_label
from .config import settings
from .database import answer_from_database
from .retriever import retrieve
from .store import Index

ROUTER_PROMPT = """Определи, где искать ответ на вопрос сотрудника.
ДОКУМЕНТЫ — правила, порядок действий, требования, нормы, сроки по регламентам, телефоны и ответственные из регламентов, инструкции, технические характеристики и эксплуатация приборов по руководствам.
ДАННЫЕ — текущие записи учёта предприятия: какое оборудование сейчас в ремонте, заявки на ремонт, остатки и партии на складе, даты поверки конкретных экземпляров приборов по инвентарным номерам, количества.

Примеры:
Как оформить акт о несоответствии? — ДОКУМЕНТЫ
Кому сообщить о неисправности инструмента? — ДОКУМЕНТЫ
Какое напряжение питания у паяльной станции? — ДОКУМЕНТЫ
Можно ли работать с ЭРИ без антистатического браслета? — ДОКУМЕНТЫ
Какие требования к оформлению приказа? — ДОКУМЕНТЫ
Какие станки сейчас не работают? — ДАННЫЕ
Когда поверять штангенциркуль СИ-0018? — ДАННЫЕ
Сколько припоя осталось на складе? — ДАННЫЕ
Какие партии поступили на склад на этой неделе и какой у них статус? — ДАННЫЕ
Какие заявки на ремонт ещё не выполнены? — ДАННЫЕ

Ответь одним словом: ДОКУМЕНТЫ или ДАННЫЕ.
{question} —"""


@dataclass
class AgentResult:
    question: str
    role: str
    route: str                     # documents | database
    status: str                    # answer | no_data | model_refused | unverified | error
    text: str
    sources: list = field(default_factory=list)   # [{n, label, text, cited, score}]
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.status == "answer"


class Agent:
    def __init__(self, index: Index | None = None):
        self.index = index or Index.load(settings.index_dir)

    def route(self, question: str) -> str:
        try:
            text, _ = llm.chat([{"role": "user", "content": ROUTER_PROMPT.format(question=question)}], max_tokens=5)
        except llm.ModelError:
            return "documents"
        return "database" if "ДАННЫЕ" in text.upper() else "documents"

    def ask(self, question: str, role: str = "Администратор", route: str = "auto") -> AgentResult:
        t0 = time.time()
        allowed = settings.roles.get(role, {"all"})
        auto = route == "auto"
        if auto:
            route = self.route(question)

        result = db_result = None
        if route == "database":
            a = answer_from_database(question, settings.db_path)
            db_result = AgentResult(question, role, route, a.status, a.text, sql=a.sql,
                                    columns=a.columns, rows=a.rows, stats=a.stats)
            if a.status == "answer" and a.rows:
                result = db_result
            else:
                route = "documents"   # в базе записей нет — проверяем документы
        if result is None:
            retrieval = retrieve(self.index, question, allowed)
            a = answer_from_documents(question, retrieval)
            sources = [{"n": n, "label": source_label(h), "text": h.chunk.text, "doc_id": h.chunk.doc_id,
                        "clause": h.chunk.clause, "page": h.chunk.page_start, "page_end": h.chunk.page_end,
                        "cited": n in a.cited, "score": round(h.score, 3)}
                       for n, h in enumerate(a.hits, 1)]
            result = AgentResult(question, role, route, a.status, a.text, sources=sources, stats=a.stats)
            if auto and db_result is None and a.status == "no_data":
                # в документах нет ничего даже отдалённо похожего — второй шанс: учётная база
                b = answer_from_database(question, settings.db_path)
                if b.status == "answer" and b.rows:
                    result = AgentResult(question, role, "database", b.status, b.text, sql=b.sql,
                                         columns=b.columns, rows=b.rows, stats=b.stats)
            elif not result.answered and db_result and db_result.status == "answer":
                result = db_result   # «записей нет» из базы — тоже ответ (например, «на складе таких нет»)

        result.seconds = round(time.time() - t0, 2)
        self._log(result)
        return result

    @staticmethod
    def _log(r: AgentResult) -> None:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "role": r.role, "question": r.question,
                  "route": r.route, "status": r.status, "answer": r.text, "seconds": r.seconds,
                  "cited": [s["label"] for s in r.sources if s["cited"]], "sql": r.sql}
        with open(settings.log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

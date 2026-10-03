"""Агент: понимает вопрос в контексте диалога, выбирает источник (документы, учётная база
или оба), отвечает и пишет журнал."""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import yaml

from . import demo_db, llm
from .answer import answer_from_documents, source_label
from .cards import build_cards, card_chunk, card_text
from .config import settings
from .database import answer_from_database
from .retriever import Hit, Retrieval, retrieve
from .store import Index
from .text import tokenize

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

CONDENSE_PROMPT = """Сотрудник продолжает разговор с помощником по документам предприятия.
Перепиши последний вопрос сотрудника так, чтобы он был понятен без предыдущих реплик: подставь, о чём идёт речь.
Если вопрос и так понятен, верни его без изменений. Не отвечай на вопрос. Верни только переписанный вопрос одной строкой.

Предыдущие реплики:
{dialog}

Последний вопрос: {question}
Переписанный вопрос:"""

CLARIFY_PROMPT = """Вопрос сотрудника слишком общий: «{question}».
В документах предприятия по нему нашлись фрагменты на разные темы:
{topics}

Предложи 3 уточнённых вопроса, которые сотрудник мог иметь в виду. Каждый вопрос сохраняет смысл исходного
и добавляет предмет из своей темы списка. Например, для вопроса «Какая температура?» — «Какая температура
хранения ЭРИ на складе?». По одному вопросу в строке, без нумерации и пояснений."""

# инвентарный, номенклатурный номер или номер партии
RECORD_RE = re.compile(r"\b(?:СИ|ОБ|ЭРИ|КИ|МТ|П)-\d{3,4}(?:-\d{2})?\b", re.I)
# вопрос о правиле: что делать, можно ли, кто должен… — вместе с номером записи даёт маршрут «оба источника»
RULE_RE = re.compile(r"можно ли|нужно ли|надо ли|требуется ли|положено ли|разрешается|допускается|запрещ|"
                     r"в какой срок|в течение какого|за какое время|в каком порядке|"
                     r"что (?:делать|нужно|необходимо|требуется)|как (?:быть|поступить|оформить|действовать)|"
                     r"порядок|кто (?:должен|подписывает|оформляет|утверждает|отвечает|принимает)|обязан|"
                     r"имею ли право", re.I)
FOLLOWUP_RE = re.compile(
    r"^(?:а|и|но|тогда|ещё|еще)\b|\b(?:это|этого|этому|этом|этот|эта|эту|эти|этих|он|она|оно|они|его|её|ее|их|"
    r"него|неё|нее|ним|ней|нём|нем|там|туда|тот|та|те|того|такой|такое|такие|такая)\b", re.I)


# вопросы о составе базы знаний и о документе целиком
DOCS_Q_RE = re.compile(
    r"\b(?:какие|какой|какая|список|перечень|перечисли)\s+(?:(?:есть|имеются|у вас|у нас|в базе)\s+)*"
    r"(?:документ\w*|регламент\w*|инструкци\w*|руководств\w*|стандарт\w*|гост\w*|нормативн\w*)|"
    r"в каких документах|есть ли (?:у вас |в базе )?(?:документ|инструкци|регламент|руководств)", re.I)
SUMMARY_Q_RE = re.compile(
    r"\bо ч[её]м\b|кратк\w* содержани|кратко (?:опиши|перескажи|расскажи)|что за документ|"
    r"что (?:содержит|описывает|устанавливает|регулирует)|для чего (?:нужен|предназначен)", re.I)
def _load_glossary() -> dict[str, list[str]]:
    path = settings.raw_dir.parent / "glossary.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("terms", {}) or {}


GLOSSARY = _load_glossary()
TOPIC_NOISE_RE = re.compile(r"\b(?:есть|имеются|у вас|у нас|в базе|про|по теме|на тему|о|об|по|касаются|касающиеся|"
                            r"описан\w*|говорится|упоминается|рассказывается|написано)\b", re.I)


@dataclass
class AgentResult:
    question: str
    role: str
    route: str                     # documents | database | both
    status: str                    # answer | no_data | model_refused | unverified | clarify | error
    text: str
    sources: list = field(default_factory=list)   # [{n, label, text, cited, score, ...}]
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)
    asked: str | None = None       # вопрос, переписанный с учётом диалога (если отличается)
    options: list = field(default_factory=list)   # варианты уточнения для status == clarify
    labels: list = field(default_factory=list)    # русские подписи столбцов rows

    @property
    def answered(self) -> bool:
        return self.status == "answer"


def _sources(a) -> list[dict]:
    return [{"n": n, "label": source_label(h), "text": h.chunk.text, "doc_id": h.chunk.doc_id,
             "doc_kind": h.chunk.doc_kind, "clause": h.chunk.clause, "page": h.chunk.page_start,
             "page_end": h.chunk.page_end, "cited": n in a.cited, "score": round(h.score, 3)}
            for n, h in enumerate(a.hits, 1)]


class Agent:
    def __init__(self, index: Index | None = None):
        self.index = index or Index.load(settings.index_dir)
        self._cards, self._cards_key = {}, None

    # ---------- документы целиком ----------

    @property
    def cards(self) -> dict[str, dict]:
        """Карточки документов; пересобираются, если индекс изменился (загрузка, удаление, отмена редакции)."""
        key = (len(self.index.chunks), self.index.chunks[-1].id if self.index.chunks else None,
               tuple(sorted({(c.doc_id, c.access, c.doc_kind) for c in self.index.chunks})))
        if key != self._cards_key:
            self._cards, self._cards_key = build_cards(self.index), key
        return self._cards

    def _ranked_cards(self, query: str, allowed: set[str]) -> list[tuple[float, dict]]:
        cards = [c for c in self.cards.values() if c["access"] in allowed]
        if not cards:
            return []
        scores = llm.rerank(query, [card_text(c)[:1500] for c in cards])
        return sorted(zip(scores, cards), key=lambda x: -x[0])

    def answer_catalog(self, question: str, role: str, allowed: set[str]) -> "AgentResult":
        """«Какие документы есть про паяльные станции?» — список из каталога, без генерации текста."""
        topic = re.sub(r"[?!.,]+", " ", TOPIC_NOISE_RE.sub(" ", DOCS_Q_RE.sub(" ", question))).strip()
        if tokenize(topic):
            topic_words = set(tokenize(topic))
            extra = [w for term, syn in GLOSSARY.items() if set(tokenize(term)) <= topic_words for w in syn]
            query = f"{topic} ({', '.join(extra)})" if extra else topic
            ranked = self._ranked_cards(query, allowed)
            best = ranked[0][0] if ranked else 0.0
            chosen = [(s, c) for s, c in ranked if s >= max(0.1, best * 0.2)]
            # тема может быть в тексте, а не в названии («по метрологии» — РИ ДП-04 «…средствами измерений»):
            # добавляем документы, фрагменты которых уверенно находит обычный поиск по теме
            have = {c["doc_id"] for _, c in chosen}
            for h in retrieve(self.index, query, allowed).hits:
                if not h.neighbor and h.score >= 0.3 and h.chunk.doc_id not in have and h.chunk.doc_id in self.cards:
                    have.add(h.chunk.doc_id)
                    chosen.append((h.score, self.cards[h.chunk.doc_id]))
            head = f"Документы по теме «{topic}»:" if chosen else f"Документов по теме «{topic}» в базе знаний нет."
        else:
            chosen = [(1.0, c) for c in sorted(self.cards.values(), key=lambda c: (c["kind"], c["title"]))
                      if c["access"] in allowed]
            head = "Документы в базе знаний:"
        lines = [f"{n}. {c['title']} — {c['kind'].lower()}" + (f", {c['pages']} стр." if c.get("pages") else "")
                 + f" [{n}]" for n, (_, c) in enumerate(chosen, 1)]
        sources = [{"n": n, "label": c["title"], "text": card_text(c), "doc_id": c["doc_id"], "doc_kind": c["kind"],
                    "clause": None, "page": None, "page_end": None, "cited": True, "score": round(s, 3)}
                   for n, (s, c) in enumerate(chosen, 1)]
        status = "answer" if chosen else "no_data"
        return AgentResult(question, role, "documents", status, "\n".join([head] + lines), sources=sources,
                           stats={"catalog": True})

    def answer_summary(self, question: str, role: str, allowed: set[str]) -> "AgentResult | None":
        """«О чём ГОСТ Р 2.105-2019?» — пересказ по карточке документа (область применения и разделы)."""
        ranked = self._ranked_cards(question, allowed)
        if not ranked or ranked[0][0] < 0.3:
            return None            # документ не назван явно — обычный поиск по фрагментам
        score, card = ranked[0]
        retrieval = Retrieval(hits=[Hit(card_chunk(card), score, None, None)], best_score=score,
                              confident=True, scored_by="reranker")
        a = answer_from_documents(question, retrieval)
        return AgentResult(question, role, "documents", a.status, a.text, sources=_sources(a),
                           stats={**a.stats, "summary": True})

    # ---------- понимание вопроса ----------

    def route(self, question: str) -> str:
        # «запись + правило» определяется по форме вопроса: номер записи учёта и вопрос о правиле.
        # Третий вариант в подсказке маршрутизатора сбивал небольшую модель на обычных вопросах.
        if RECORD_RE.search(question) and RULE_RE.search(question):
            return "both"
        if RULE_RE.search(question):
            return "documents"   # «что делать», «можно ли», «порядок» без номера записи — это правила
        if RECORD_RE.search(question):
            return "database"    # номер записи без вопроса о правиле — это запись учёта
        try:
            text, _ = llm.chat([{"role": "user", "content": ROUTER_PROMPT.format(question=question)}], max_tokens=5)
        except llm.ModelError:
            return "documents"
        return "database" if "ДАННЫЕ" in text.upper() else "documents"

    @staticmethod
    def needs_context(question: str) -> bool:
        return len(tokenize(question)) <= 3 or bool(FOLLOWUP_RE.search(question))

    def condense(self, question: str, history: list[dict]) -> str:
        """«А если ночью?» после вопроса об отказе оборудования → «Что делать при отказе оборудования ночью?»"""
        dialog = "\n".join(f"Сотрудник: {h['question']}\nПомощник: {h.get('text', '')[:300]}" for h in history[-3:])
        try:
            out, _ = llm.chat([{"role": "user", "content": CONDENSE_PROMPT.format(dialog=dialog, question=question)}],
                              max_tokens=80)
        except llm.ModelError:
            return question
        out = out.strip().splitlines()[0].strip(" «»\"") if out.strip() else ""
        return out if 5 <= len(out) <= 300 else question

    def clarify(self, question: str, retrieval) -> list[str]:
        # тема = документ + предложение, где встречается слово из вопроса («Гарантийный срок — 12 месяцев»)
        words, topics, seen = set(tokenize(question)), [], set()
        for h in retrieval.hits:
            if h.neighbor or h.chunk.doc_id in seen:
                continue
            seen.add(h.chunk.doc_id)
            sentences = re.split(r"(?<=[.;!?])\s+", h.chunk.text)
            hit = next((s for s in sentences if words & set(tokenize(s))), sentences[0])
            topics.append(f"- {h.chunk.doc_title}: «{hit.strip()[:160]}»")
        try:
            out, _ = llm.chat([{"role": "user", "content": CLARIFY_PROMPT.format(
                question=question, topics="\n".join(topics))}], max_tokens=150)
        except llm.ModelError:
            return []
        options = [re.sub(r"^[\s\d.)•*-]+", "", line).strip() for line in out.splitlines()]
        return [o for o in options if len(o) > 8][:3]

    @staticmethod
    def is_vague(question: str, retrieval) -> bool:
        """Короткий вопрос без предмета, а найденное разбросано по разным документам."""
        docs = {h.chunk.doc_id for h in retrieval.hits if not h.neighbor}
        return (len(tokenize(question)) <= 2 and len(docs) >= 3
                and settings.rerank_threshold <= retrieval.best_score < settings.confident_threshold)

    # ---------- ответ ----------

    def ask(self, question: str, role: str = "Администратор", route: str = "auto",
            history: list[dict] | None = None, on_step: Callable[[str], None] | None = None,
            user: str | None = None) -> AgentResult:
        t0 = time.time()
        step = on_step or (lambda _: None)
        if settings.db_path == demo_db.DB:
            demo_db.ensure_current()      # даты демо-базы — от сегодняшнего дня
        allowed = settings.roles.get(role, {"all"})

        asked = question
        if history and self.needs_context(question):
            step("Уточняю вопрос по ходу диалога…")
            asked = self.condense(question, history)

        auto = route == "auto"
        result = db_result = a_db = None
        if route in ("auto", "documents") and DOCS_Q_RE.search(asked):
            step("Ищу подходящие документы в каталоге…")
            result = self.answer_catalog(asked, role, allowed)
            route = "documents"
        elif route in ("auto", "documents") and SUMMARY_Q_RE.search(asked):
            step("Ищу документ и его описание…")
            result = self.answer_summary(asked, role, allowed)
            if result is not None:
                route = "documents"
        if result is None and auto:
            step("Выбираю, где искать ответ…")
            route = self.route(asked)

        if route in ("database", "both"):
            step("Запрос к учётной базе…")
            records = [m.upper() for m in RECORD_RE.findall(asked)] if route == "both" else None
            # похожие записи ищем, только когда вопрос явно к базе (не для второго шанса после документов)
            a = a_db = answer_from_database(asked, settings.db_path, records=records or None,
                                            near=route == "database")
            db_result = AgentResult(question, role, "database", a.status, a.text, sql=a.sql,
                                    columns=a.columns, rows=a.rows, stats=a.stats, labels=a.labels)
            if route == "database":
                if a.status == "answer" and a.rows:
                    result = db_result
                else:
                    route = "documents"   # в базе записей нет — проверяем документы

        if result is None:
            step("Ищу в документах…")
            # для «запись + правило» ищем с учётом того, что это за запись и в каком она состоянии
            summary = a_db.summary if route == "both" and a_db is not None else ""
            retrieval = retrieve(self.index, f"{asked} {summary}".strip(), allowed)
            if asked != question and history:
                # уточнение в диалоге: ищем и вместе с темой предыдущего вопроса («отказ категории Б» +
                # «ночная смена» → раздел о ночной смене), добавляем до двух новых фрагментов
                extra = retrieve(self.index, f"{asked} {history[-1]['question']}", allowed)
                seen = {h.chunk.id for h in retrieval.hits}
                retrieval.hits += [h for h in extra.hits if not h.neighbor and h.chunk.id not in seen][:2]
            prev_docs = set((history or [{}])[-1].get("docs") or [])
            if (asked != question and not retrieval.confident and retrieval.hits
                    and retrieval.hits[0].chunk.doc_id in prev_docs
                    and retrieval.best_score >= settings.rerank_threshold / 3):
                # уточнение в диалоге, и лучший фрагмент — из документа предыдущего ответа: отвечаем,
                # но как в «зоне сомнения» — с проверкой, что ответ о том же предмете
                retrieval.confident = True
            if auto and route == "documents" and self.is_vague(asked, retrieval):
                step("Вопрос слишком общий — подбираю уточнения…")
                options = self.clarify(asked, retrieval)
                if options:
                    result = AgentResult(question, role, "documents", "clarify",
                                         "Вопрос слишком общий — уточните, что именно вас интересует:",
                                         options=options, stats={"best_score": round(retrieval.best_score, 3)})
            if result is None:
                # модели — только краткое состояние записи (что это и какой статус), без дат и чисел:
                # полную запись она начинает пересказывать и достраивать; запись целиком показывается отдельно
                facts = (a_db.summary or db_result.text) if route == "both" and db_result \
                    and db_result.status == "answer" else None
                step(f"Найдено фрагментов: {len(retrieval.hits)}. Составляю ответ и проверяю ссылки…"
                     if retrieval.confident else "Подходящих фрагментов в документах нет")
                a = answer_from_documents(asked, retrieval, facts=facts)
                result = AgentResult(question, role, route, a.status, a.text, sources=_sources(a), stats=a.stats)
                if route == "both":
                    if a.answered and facts:
                        result.text = f"**По учётной базе**\n\n{db_result.text}\n\n**По документам**\n\n{a.text}"
                        result.sql, result.columns, result.rows = db_result.sql, db_result.columns, db_result.rows
                        result.labels = db_result.labels
                    elif db_result and db_result.status == "answer":
                        result = db_result   # в документах правила нет — показываем запись из базы
                    else:
                        result.route = "documents"
                elif auto and db_result is None and a.status == "no_data":
                    # в документах нет ничего даже отдалённо похожего — второй шанс: учётная база
                    step("В документах нет — проверяю учётную базу…")
                    b = answer_from_database(asked, settings.db_path)
                    if b.status == "answer" and b.rows:
                        result = AgentResult(question, role, "database", b.status, b.text, sql=b.sql,
                                             columns=b.columns, rows=b.rows, stats=b.stats, labels=b.labels)
                elif not result.answered and db_result and db_result.status == "answer":
                    result = db_result   # «записей нет» из базы — тоже ответ (например, «на складе таких нет»)

        if db_result is not None and result is not db_result and result.route == "documents":
            # для журнала и разбора ошибок: к базе обращались, но ответ взят из документов
            result.stats.update(db_status=db_result.status, db_sql=db_result.sql, db_rows=len(db_result.rows))
        result.question, result.role = question, role
        result.asked = asked if asked != question else None
        result.seconds = round(time.time() - t0, 2)
        self._log(result, user)
        return result

    @staticmethod
    def _log(r: AgentResult, user: str | None) -> None:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "role": r.role, "question": r.question,
                  "asked": r.asked, "route": r.route, "status": r.status, "answer": r.text, "seconds": r.seconds,
                  "cited": [s["label"] for s in r.sources if s["cited"]], "sql": r.sql}
        with open(settings.log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

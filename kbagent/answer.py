"""Ответ по найденным фрагментам со ссылками и тремя уровнями защиты от выдумок.

1. Если поиск не нашёл ничего достаточно похожего — отказ без вызова модели.
2. Модель получает только найденные фрагменты и инструкцию отвечать по ним,
   а при отсутствии ответа — вернуть маркер НЕТ_ДАННЫХ.
3. Ответ проверяется программно: каждая ссылка [n] должна указывать на
   выданный модели фрагмент; ответ без единой верной ссылки не показывается.
"""
import re
from dataclasses import dataclass, field

from . import llm
from .retriever import Hit, Retrieval

NO_DATA = "НЕТ_ДАННЫХ"

SYSTEM_PROMPT = f"""Ты — помощник сотрудников производственного предприятия. Ты отвечаешь на вопросы ТОЛЬКО по фрагментам внутренних документов, которые приведены ниже.

Правила:
1. Ответ должен содержать конкретные сведения из фрагментов: сроки, числа, порядок действий, требования. Сначала суть, затем кратко — на каком требовании документа она основана.
2. Каждое утверждение подкрепляй ссылкой на номер фрагмента в квадратных скобках, например [2]. Ответ без ссылок недопустим.
3. Учитывай, что в вопросе и в документе одно и то же может называться по-разному: прибор — средство измерений (СИ), комплектующие — ЭРИ, неисправность — отказ. Ответ может складываться из нескольких фрагментов.
4. Не используй собственные знания и ничего не додумывай. Числа, сроки, телефоны, номера форм и названия переписывай точно как во фрагментах.
5. Если ни один фрагмент не позволяет ответить на вопрос, ответь ровно одним словом: {NO_DATA}
6. Отвечай кратко, на русском языке. Последовательность действий оформляй нумерованным списком.

Пример формата (вопрос не из этих документов):
Вопрос: В какой срок подаётся заявка на пропуск для посетителя?
Ответ: Заявку на пропуск подают не позднее чем за 1 рабочий день до визита; в заявке указывают ФИО посетителя и цель визита [2].
"""


@dataclass
class Answer:
    status: str                    # answer | no_data | model_refused | unverified | error
    text: str
    cited: list[int] = field(default_factory=list)   # номера фрагментов (с 1), на которые есть ссылки
    hits: list[Hit] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.status == "answer"


def source_label(hit: Hit) -> str:
    c = hit.chunk
    parts = [c.doc_title]
    if c.clause:
        parts.append(f"п. {c.clause}")
    if c.page_start:
        pages = c.page_start if c.page_start == c.page_end else f"{c.page_start}–{c.page_end}"
        parts.append(f"стр. {pages}")
    return ", ".join(parts)


def _context(hits: list[Hit]) -> str:
    blocks = []
    for n, hit in enumerate(hits, 1):
        c = hit.chunk
        head = f"[{n}] Документ: {c.doc_title}"
        if c.heading:
            head += f"; раздел: {c.heading}"
        if c.clause:
            head += f"; пункт {c.clause}"
        if c.page_start:
            head += f"; стр. {c.page_start}"
        blocks.append(f"{head}\n{c.text}")
    return "\n\n".join(blocks)


def _valid_refs(text: str, n_hits: int) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 1 <= int(n) <= n_hits})


def _too_short(text: str) -> bool:
    bare = re.sub(r"\[\d+\]|[\s.,!]", "", text)
    return NO_DATA not in text and len(bare) < 12


def answer_from_documents(question: str, retrieval: Retrieval) -> Answer:
    hits = retrieval.hits
    stats = {"best_score": round(retrieval.best_score, 3), "scored_by": retrieval.scored_by}
    if not retrieval.confident:
        return Answer("no_data", "В доступных документах ответа не нашёл.", hits=hits, stats=stats)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Фрагменты документов:\n\n{_context(hits)}\n\nВопрос: {question}\n\n"
                                    "Ответь по существу: приведи конкретные сведения из фрагментов (сроки, числа, "
                                    f"действия, требования) со ссылками [n]. Если ответа во фрагментах нет — {NO_DATA}."},
    ]
    try:
        text, gen = llm.chat(messages)
        if _too_short(text):
            # односложный ответ вроде «Да [1].» на вопрос «в какой срок…» — просим конкретику
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Ответ слишком краткий. Перепиши его полным предложением, "
                                                     "приведя конкретные сведения из фрагмента со ссылкой [n]."}]
            text, gen2 = llm.chat(messages)
            gen["seconds"] = round(gen["seconds"] + gen2["seconds"], 2)
    except llm.ModelError as e:
        return Answer("error", f"Языковая модель недоступна: {e}", hits=hits, stats=stats)
    stats.update(gen)

    if NO_DATA in text or not text:
        return Answer("model_refused", "В найденных фрагментах ответа нет.", hits=hits, stats=stats)

    valid = _valid_refs(text, len(hits))
    if not valid:
        # ответ дан, но без ссылки — один раз просим указать фрагмент; не указал — ответ не показываем
        try:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Укажи в ответе ссылки [n] на фрагменты, на которых он основан. "
                                                     f"Если ответ не следует из фрагментов — ответь {NO_DATA}."}]
            text, gen2 = llm.chat(messages)
            stats["seconds"] = round(stats.get("seconds", 0) + gen2["seconds"], 2)
        except llm.ModelError:
            pass
        if NO_DATA in text:
            return Answer("model_refused", "В найденных фрагментах ответа нет.", hits=hits, stats=stats)
        valid = _valid_refs(text, len(hits))
    # убираем ссылки на несуществующие фрагменты
    text = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    if not valid or _too_short(text):
        return Answer("unverified", "Не удалось подтвердить ответ ссылкой на документ.", hits=hits, stats=stats)
    return Answer("answer", text.strip(), cited=valid, hits=hits, stats=stats)

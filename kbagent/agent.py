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
from .editions import compare, edition_pairs
from .normocontrol import GOST_ID, check_docx
from .privacy import mask, prune
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
RULE_RE = re.compile(r"можно ли|можно\b|нужно ли|надо ли|требуется ли|положено ли|разрешается|допускается|запрещ|"
                     r"кому (?:звонить|сообщ\w*|обращаться|докладывать)|куда (?:звонить|сообщ\w*|обращаться)|"
                     r"в какой срок|в течение какого|за какое время|в каком порядке|длится|как (?:быстро|скоро)|"
                     r"через (?:сколько|какое время)|за сколько (?:дней|часов|минут|суток|недель|месяцев)\b|"
                     r"уведомля\w*|сколько действует|срок действия|"
                     r"сколько\s[^?]*\b(?:осмотреть|проверить|отобрать|испытать|проконтролировать)\b|"
                     r"сколько стоит|стоимост|\bцен[аыуе]\b|расценк|тариф|"
                     r"сколько\s[^?]*\b(?:нужно|надо|следует|требуется)\b|"     # «сколько штук нужно осмотреть» — расчёт по правилу
                     r"(?:сколько|скока|скоко|сколко)\s+(?:дней|часов|минут|суток|недель|месяцев|лет)\b|"
                     r"что (?:делать|нужно|необходимо|требуется)|как (?:быть|поступить|оформить|действовать)|"
                     r"порядок|кто (?:должен|подписывает|оформляет|утверждает|отвечает|принимает)|обязан|"
                     r"имею ли право|"
                     # «где проводится поверка», «как осуществляется приёмка» — о порядке, а не о записях учёта
                     r"\b(?:проводи|проводя|осуществля|выполня|производи|производя)\w*?(?:тся|ться)\b", re.I)
# вопрос к учёту по форме: где лежит, что на складе, сколько осталось — без модели-маршрутизатора
STOCK_RE = re.compile(r"где\s+(?:лежит|лежат)\b|на складе|в наличии|\bостат(?:ок|ки|ка|ков)\b|сколько осталось", re.I)
# «Откуда это?», «Где это написано?», «А ты уверен?» — о предыдущем ответе: тот же вопрос, те же источники
SOURCE_Q_RE = re.compile(r"^\s*(?:а\s+|и\s+)?(?:откуда (?:это|ты (?:это )?(?:взял|знаешь)|информация|такие данные)|"
                         r"где (?:это )?(?:написано|сказано|указано)|(?:какой|каков) источник|на что (?:ты )?ссылаешься|"
                         r"ты уверен\w*|точно)\s*[?!.]*\s*$", re.I)
# «А если ночью?», «А при категории В?» после ответа по документам — уточнение того же правила
CONDITION_RE = re.compile(r"^\s*(?:а|и)\s+(?:если|когда|при|в случае|ночью|в выходн\w*|в праздни\w*)\b", re.I)
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
# «Что изменилось в новой редакции СТП ДП-02?» — сравнение редакций по пунктам
EDITION_Q_RE = re.compile(r"что (?:изменилось|поменялось|нового|изменено|поменяли|изменили|обновили)|"
                          r"как\w* изменения|"
                          r"изменени\w+ (?:в|по сравнению)|(?:сравни|сравнить|сравнение)\w*\s+(?:редакци|верси)|"
                          r"чем (?:отличается|отличаются)\s+(?:нов\w+|стар\w+|прежн\w+|действующ\w+)\s+(?:редакци|верси)|"
                          r"отличи\w+ (?:редакций|версий|от прежней|от старой)", re.I)
EDITION_LIST = 12          # сколько добавленных пунктов перечислять, остальные — числом
# местоимение во второй части («…и куда её перемещают?», «как быстро на них реагируют?») отсылает к первой —
# такую часть не отвечают отдельно
ANAPHORA_RE = re.compile(r"\b(?:них|ним|ними|их|него|нему|нём|неё|нее|ней|его|ему|им|её|ее|он|она|оно|они|это|этого|этому|этим|там|тогда)\b", re.I)

# Разговорные реплики и вопросы о самом агенте: отвечаем справкой, а не поиском по документам
GREETING_RE = re.compile(r"^\s*(?:привет\w*|здравствуй\w*|добр\w+\s+(?:день|утро|вечер)|доброе утро|hello|hi|хай)"
                         r"[\s!.,)]*$", re.I)
THANKS_RE = re.compile(r"^\s*(?:спасибо|благодарю|спс|понятно|ясно|ок|окей|хорошо)[\s!.,)]*(?:большое)?[\s!.,)]*$",
                       re.I)
HELP_RE = re.compile(r"что (?:ты )?(?:умеешь|можешь)|кто ты|ты кто|как (?:тобой|с тобой) (?:пользоваться|работать)|"
                     r"^\s*(?:помощь|справка|help)\s*\??\s*$", re.I)
ABOUT_RE = re.compile(r"откуда (?:ты )?(?:бер[её]шь|зна[её]шь|ответ|информаци|данные)|на ч[её]м (?:ты )?основ|"
                      r"как ты (?:работаешь|находишь|отвечаешь)|можно (?:ли )?тебе доверять|ты (?:не )?выдумываешь",
                      re.I)
# Вопросы о самом агенте, которые задают на показе: модель, интернет, ошибки, куда уходят вопросы,
# служебные настройки, просьбы изменить базу, совет «от себя», стихи и анекдоты
MODEL_Q_RE = re.compile(r"chat\s*gpt|\bgpt\b|чат\s*гпт|\bгпт\b|gigachat|гигачат|кто тебя (?:сделал|создал|разработал)|"
                        r"(?:как\w*|на как\w*)\s+(?:ты\s+)?(?:модел\w*|нейросет\w*|ии\b|искусственн\w+)|"
                        r"\bты\s+(?:[—-]\s*)?(?:нейросеть|ии|бот|робот|человек)\b", re.I)
OFFLINE_Q_RE = re.compile(r"\b(?:ты|агент\w*|систем\w*|помощник\w*|работаешь|работает)\b[^?]{0,40}\bинтернет|"
                          r"нуж\w+ ли (?:тебе |для работы )?интернет|\bоблак|"
                          r"(?:переда[её]шь|отправляешь|отсылаешь)\b[^?]{0,30}(?:наружу|интернет|сервер|облак)", re.I)
ERRORS_Q_RE = re.compile(r"\b(?:ты|вы)\s+(?:можешь|может|можете)\s+(?:ли\s+)?ошиб|(?:можешь|бывает)\s+ли\s+(?:ты\s+)?ошиб|"
                         r"\bты\s+ошибаешься|насколько\s+(?:ты\s+)?точ\w+", re.I)
PRIVACY_Q_RE = re.compile(r"куда\s+уход\w*|кто\s+(?:видит|читает|смотрит)\s+(?:мои\s+)?(?:вопрос|ответ|запрос|диалог)|"
                          r"(?:сохраня|храни|храня|записыва)\w*\s+(?:ли\s+)?(?:ты\s+)?(?:мои\s+)?(?:вопрос|запрос|диалог)|"
                          r"конфиденциальн|\bмои\s+(?:вопросы|запросы|данные)\b", re.I)
PROMPT_Q_RE = re.compile(r"системн\w*\s+(?:промпт|подсказк|инструкц)|(?:свой|свои|твой|твои)\s+(?:промпт|инструкци\w*|"
                         r"настройк\w*)|\bprompt\b|\bпромпт", re.I)
WRITE_Q_RE = re.compile(r"^\s*(?:удали|удалить|измени|изменить|добавь|добавить|исправь|исправить|внеси|внести|обнови|"
                        r"обновить|создай|создать|оформи|оформить|переведи|перевести|поменяй|поменять|смени|сменить|"
                        r"установи|установить|выставь|выставить|спиши|списать)\b[^?]*(?:запис|заявк|баз|уч[её]т|склад|остат|прибор|"
                        r"оборудован|парти|позици)|\b(?:delete|update|insert|drop|truncate|alter)\b", re.I)
OPINION_Q_RE = re.compile(r"лучше\s+(?:купить|выбрать|взять|заказать)|для\s+дома|посоветуй|порекомендуй|"
                          r"тво[её]\s+мнение|как\s+(?:ты\s+)?думаешь|стоит\s+ли\s+(?:покупать|брать|заказывать)", re.I)
CREATIVE_Q_RE = re.compile(r"^\s*(?:напиши|сочини|придумай|расскажи)\s+(?:мне\s+)?(?:стих\w*|анекдот\w*|шутк\w*|"
                           r"сказк\w*|песн\w*|историю|рассказ)", re.I)
MODEL_TEXT = ("Я работаю на открытой отечественной языковой модели GigaChat 3.1 Lightning, установленной прямо на "
              "компьютере предприятия. ChatGPT и другие внешние сервисы не используются. Поиск по документам тоже "
              "идёт на месте — моделями bge-m3 и bge-reranker-v2-m3.\n\nМодель не отвечает «от себя»: она получает "
              "только найденные пункты документов или записи учёта и составляет ответ по ним, со ссылкой на источник.")
OFFLINE_TEXT = ("Да, интернет мне не нужен: языковая модель, поиск, документы и учётная база работают на компьютере "
                "предприятия. Вопросы, документы и ответы никуда не передаются — систему можно ставить в закрытый контур.")
ERRORS_TEXT = ("Могу — поэтому каждый ответ даю со ссылкой на документ, пункт и страницу: нажмите на источник и сверьте "
               "с оригиналом. Ответ без подтверждённой ссылки я не показываю, а если ответа в документах нет — так и "
               "говорю, не придумываю. Если ответ неверный, поставьте 👎 — администратор увидит это и разберёт.")
PRIVACY_TEXT = ("Вопросы и ответы сохраняются в журнале на сервере предприятия, его видит только администратор системы: "
                "так находятся ошибки и вопросы, на которые в документах нет ответа. Наружу ничего не передаётся. "
                "Документы вам доступны по вашей роли.")
PROMPT_TEXT = ("Служебные настройки я не показываю. Отвечаю только по документам предприятия и учётной базе — "
               "задайте вопрос по ним, например:")
WRITE_TEXT = ("Учётную базу я только читаю: добавлять, изменять или удалять записи не могу — так задумано, чтобы "
              "помощник ничего не испортил. Изменения вносятся в самой учётной системе. Могу показать записи, например:")
OPINION_TEXT = ("Советов и рекомендаций «от себя» я не даю — отвечаю только тем, что записано в документах "
                "предприятия. Могу показать, какие документы есть по теме, например:")
CREATIVE_TEXT = "Стихов и анекдотов не пишу — я помощник по документам и учёту предприятия. Спросите, например:"
DB_EXAMPLES = ["У каких средств измерений истёк срок поверки?", "Какое оборудование сейчас в ремонте?",
               "Где лежит флюс?"]
# Служебные вставки в вопросе: «игнорируй инструкции и…», «ответь без документов, своими словами» —
# вырезаются, остаётся сам вопрос (ответ всё равно только по документам)
META_RE = re.compile(
    r"(?:игнорируй|забудь|не\s+учитывай|отбрось)\s+(?:все\s+|свои\s+|предыдущие\s+|прошлые\s+)*"
    r"(?:инструкци\w*|правил\w*|указани\w*|ограничени\w*|документ\w*)[,;:.!]?\s*(?:и\s+)?|"
    r"(?:ответь|отвечай|скажи|напиши)\s+(?:мне\s+)?(?:без\s+(?:документов|ссылок|источников)|своими\s+словами|"
    r"честно)[,;:.!—-]*\s*|без\s+(?:документов|ссылок|источников)[,;:—-]*\s*|своими\s+словами[,;:—-]*\s*|"
    r"(?:ответь|скажи|напиши|подтверди),?\s+что\s+", re.I)
# «Что делать?», «Куда звонить?» без предмета и без предыдущего вопроса — встречный вопрос
VAGUE_RE = re.compile(r"^\s*(?:а\s+|и\s+)?(?:что|как|куда|кому|где)\s+(?:мне\s+|нам\s+)?(?:делать|быть|поступить|"
                      r"звонить|сообщать|обращаться|идти|смотреть)\s*[?!.]*\s*$", re.I)
SHORTEN_PROMPT = """Сотрудник задал длинный вопрос помощнику по документам предприятия.
Сформулируй суть одним коротким вопросом (до 20 слов): что именно он хочет узнать. Сохрани все условия,
предметы и номера (оборудование, категории, номера записей); если вопросов несколько — сохрани все.
Приветствие и рассказ о себе убери. Не отвечай на вопрос. Верни только вопрос одной строкой.

Вопрос: {question}
Короткий вопрос:"""
LONG_QUESTION_WORDS = 22

EXAMPLES = ["Что делать при искрении оборудования?", "На какой срок выдаётся наряд-допуск?",
            "Можно ли выдать в производство партию П-2609-04?", "Какие документы есть про паяльные станции?"]
ABOUT_TEXT = ("Я отвечаю только по документам предприятия, доступным вашей роли, и по учётной базе — "
              "без интернета и без собственных знаний модели.\n\n"
              "1. По вопросу ищу подходящие пункты документов — по словам и по смыслу — и оцениваю, отвечают ли они на вопрос.\n"
              "2. Ответ составляю только из найденных пунктов; каждое утверждение — со ссылкой [n] на документ, пункт и "
              "страницу. Нажмите на источник, чтобы увидеть фрагмент в оригинале.\n"
              "3. Ответ без подтверждённой ссылки не показываю. Если ответа в документах нет — так и говорю.\n"
              "4. Вопросы по учёту (оборудование, приборы, склад, заявки) — запрос к учётной базе только на чтение; "
              "записи показываю таблицей.")


PART_SPLIT_RE = re.compile(
    r"\s*[,;]?\s+и\s+(?=(?:(?:к|в|на|о|об|по|с|у|от|для|из|при)\s+)?(?:как|что|кто|какой|какая|какое|какие|каким|"
    r"где|когда|сколько|куда|кому|кого|чем|почему|зачем|в течение|за какое)\b)|\?\s+(?=\S)", re.I)


# Как вопрос называет документ → как это записано в названии документа
DOC_CODES = [
    (re.compile(r"гост\w*\s*(?:р\s*)?(\d+(?:\.\d+)+)", re.I), lambda v: rf"гост[^,«]*?\b{re.escape(v)}\b"),
    (re.compile(r"\b(стп|ри)\s*дп[-\s]*(\d{1,2})\b", re.I), lambda kv: rf"\b{kv[0].lower()}\s*дп-?0?{int(kv[1])}\b"),
    (re.compile(r"\b(\d{3})\s*н\b", re.I), lambda v: rf"\b{v}н\b"),
    (re.compile(r"\bакип[-\s]*(\d{4})", re.I), lambda v: rf"акип-{v}"),
]
MODEL_RE = re.compile(r"\b(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{3,}\b")       # MAS830L, SBK936B, TDM, Lukey


def named_docs(question: str, cards: dict[str, dict]) -> set[str]:
    """Документы, которые вопрос называет прямо: «по ГОСТ 7.0.97», «СТП ДП-01», «у мультиметра MAS830L»."""
    patterns = [make(m) for rx, make in DOC_CODES for m in rx.findall(question)]
    patterns += [rf"(?<![a-z0-9]){re.escape(m.lower())}" for m in MODEL_RE.findall(question)
                 if m.lower() not in {"and", "the", "what", "for", "how", "is"}]
    if not patterns:
        return set()
    out = {doc_id for doc_id, card in cards.items()
           if any(re.search(p, card["title"].lower().replace("ё", "е")) for p in patterns)}
    return out if len(out) < len(cards) else set()


def merge(base, extra, limit: int) -> None:
    """Добавляет к найденному до `limit` новых уверенных фрагментов из другого поиска."""
    seen = {h.chunk.id: h for h in base.hits}
    for h in extra.hits:
        # тот же фрагмент по части вопроса оценён выше («порядок действий при искрении» — 0,18 против 0,01
        # по всему длинному вопросу): берём лучшую оценку, иначе ответ по нему сочтётся опорой на слабое
        old = seen.get(h.chunk.id)
        if old is not None and not h.neighbor and h.score > old.score:
            old.score, old.neighbor = h.score, False
    new = [h for h in extra.hits if not h.neighbor and h.chunk.id not in seen
           and h.score >= settings.rerank_threshold][:limit]
    base.hits += new
    if extra.best_score > base.best_score:
        base.best_score, base.confident = extra.best_score, extra.confident


def split_parts(question: str) -> list[str]:
    """«Чем отличаются категории А, Б и В и как быстро на них реагируют?» → две части
    («… А, Б и В» не делится: после «и» нет вопросительного слова)."""
    if re.search(r"[.!]\s+[А-ЯЁA-Z]", question.strip().rstrip("?")):
        return [question]          # вводное предложение («Пришла партия…») нужно обеим частям
    parts = [p.strip(" ,;?") for p in PART_SPLIT_RE.split(question)]
    parts = [p for p in parts if len(tokenize(p)) >= 2]
    return parts if len(parts) > 1 else [question]


def _load_glossary() -> dict[str, list[str]]:
    path = settings.raw_dir.parent / "glossary.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("terms", {}) or {}


GLOSSARY = _load_glossary()
TOPIC_NOISE_RE = re.compile(r"\b(?:в базе знаний|базе знаний|база знаний|в системе|у тебя|вообще|всего|доступн\w*|"
                            r"ты знаешь|знаешь|есть|имеются|у вас|у нас|в базе|про|по теме|на тему|о|об|по|касаются|касающиеся|"
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
        prune(settings.log_dir / "audit.jsonl")       # журнал вопросов — не дольше срока хранения

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

    def answer_editions(self, question: str, role: str, allowed: set[str]) -> "AgentResult | None":
        """«Что изменилось в СТП ДП-02?» — изменённые, добавленные и исключённые пункты новой редакции
        по сравнению с прежней, со ссылками. Прежней редакции нет — честный отказ без вызова модели."""
        pairs = {new: old for new, old in edition_pairs().items()
                 if new in self.cards and self.cards[new]["access"] in allowed}
        named = named_docs(question, self.cards)
        old_to_new = {old: new for new, old in pairs.items()}
        chosen = [d for d in named if d in pairs] + [old_to_new[d] for d in named if d in old_to_new]
        if not chosen and not named:
            # документ не назван — берём тот, что поиск поставил первым, если у него есть прежняя редакция
            # («Что поменяли в регламенте о действиях при отказе оборудования?» → СТП ДП-02)
            hits = retrieve(self.index, question, allowed).hits
            top = hits[0].chunk.doc_id if hits else None
            chosen = [top] if top in pairs else [old_to_new[top]] if top in old_to_new else []
        if not chosen:
            # «Что изменилось в ГОСТ Р 2.105-2019?» — прежней редакции в базе нет: сравнивать не с чем,
            # и модели этот вопрос не отдаём — она пересказала бы текущий текст как «изменения»
            # название — только у документа, доступного роли: закрытый документ не подтверждаем даже названием
            titles = [self.cards[d]["title"] for d in sorted(named) if self.cards.get(d, {}).get("access") in allowed]
            subject = f"документа «{titles[0]}»" if len(titles) == 1 else "этого документа"
            available = "; ".join(f"«{self.cards[n]['title']}»" for n in sorted(pairs))
            text = (f"Прежней редакции {subject} в доступных документах нет — сравнивать не с чем, поэтому "
                    "об изменениях сказать не могу." + (f" Сравнение редакций доступно для: {available}."
                                                        if available else ""))
            return AgentResult(question, role, "documents", "no_data", text)
        new_id = chosen[0]
        cmp = compare(pairs[new_id], new_id)
        sources, lines = [], []

        def ref(clause, doc_id, title) -> str:
            n = len(sources) + 1
            label = f"{title}, п. {clause.number}" + (f", стр. {clause.page}" if clause.page else "")
            sources.append({"n": n, "label": label, "text": clause.text, "doc_id": doc_id,
                            "doc_kind": self.cards.get(doc_id, {}).get("kind", ""), "clause": clause.number,
                            "page": clause.page, "page_end": clause.page, "cited": True, "score": 1.0})
            return f"[{n}]"

        changed = [c for c in cmp.changes if c.kind == "changed"]
        added = [c for c in cmp.changes if c.kind == "added"]
        removed = [c for c in cmp.changes if c.kind == "removed"]
        lines.append(f"Новая редакция: {cmp.new_title}. Прежняя (отменена): {cmp.old_title}.\n\n"
                     f"Изменено пунктов — {len(changed)}, добавлено — {len(added)}, исключено — {len(removed)}, "
                     f"без изменений — {cmp.unchanged}.")
        if changed:
            lines.append("\n**Изменено** (~~было~~ **стало**):")
            for c in changed:
                moved = f" (в прежней редакции — п. {c.old.number})" if c.old.number != c.new.number else ""
                lines.append(f"- п. {c.new.number}{moved}: {c.diff} {ref(c.new, new_id, cmp.new_title)}")
        if added:
            lines.append("\n**Добавлено:**")
            for c in added[:EDITION_LIST]:
                body = re.sub(r"^\s*\d{1,3}(?:\.\d{1,3})*\.?\s+", "", c.new.text)
                # добавленные пункты — без кнопок-источников: их много, а сравнить не с чем
                lines.append(f"- п. {c.new.number}: {body[:220]}{'…' if len(body) > 220 else ''}")
            if len(added) > EDITION_LIST:
                lines.append(f"- и ещё пунктов: {len(added) - EDITION_LIST}")
        if removed:
            lines.append("\n**Исключено:**")
            for c in removed:
                lines.append(f"- п. {c.old.number} прежней редакции: {c.old.text[:220]} "
                             f"{ref(c.old, cmp.old_id, cmp.old_title)}")
        if not cmp.changes:
            lines.append("\nТекст пунктов не изменился.")
        return AgentResult(question, role, "documents", "answer", "\n".join(lines), sources=sources,
                           stats={"editions": [cmp.old_id, cmp.new_id]})

    def normocontrol(self, filename: str, data: bytes, role: str = "Администратор", question: str = "",
                     user: str | None = None) -> "AgentResult":
        """Проверка оформления приложенного DOCX по ГОСТ Р 7.0.97: замечания со ссылками на пункты стандарта."""
        t0 = time.time()
        asked = f"{question.strip() or 'Проверь оформление документа'} · {filename}"
        try:
            report = check_docx(data)
        except Exception:  # noqa: BLE001 — повреждённый или не тот файл
            result = AgentResult(asked, role, "normo", "error", f"Не удалось прочитать «{filename}» как документ Word "
                                 "(DOCX). Сохраните его в Word в формате .docx и прикрепите снова.")
            result.seconds = round(time.time() - t0, 2)
            self._log(result, user)
            return result

        def part(number: str) -> tuple[int, ...]:
            return tuple(int(x) for x in re.findall(r"\d+", number))

        def chunk_for(clause: str):
            """Фрагмент стандарта с этим пунктом: номера того же уровня («3.4» — в «3.4–3.5», а не в «1–6»),
            из подходящих — с самым узким диапазоном."""
            target, found = part(clause), []
            for c in self.index.chunks:
                if c.doc_id != GOST_ID or not c.clause:
                    continue
                first, _, last = c.clause.partition("–")
                lo, hi = part(first), part(last or first)
                if len(lo) == len(hi) == len(target) and lo <= target <= hi:
                    found.append((len(c.text), c))
            return min(found, key=lambda x: x[0])[1] if found else None

        sources, refs = [], {}

        def ref(clause: str) -> str:
            c = chunk_for(clause)
            if c is None:
                return f"(п. {clause})"
            if c.id not in refs:                 # пункты 3.4 и 3.5 в одном фрагменте — одна ссылка
                refs[c.id] = len(sources) + 1
                sources.append({"n": refs[c.id], "label": source_label(Hit(c, 1.0, None, None)), "text": c.text,
                                "doc_id": c.doc_id, "doc_kind": c.doc_kind, "clause": c.clause,
                                "page": c.page_start, "page_end": c.page_end, "cited": True, "score": 1.0})
            return f"[{refs[c.id]}]"

        errors = [i for i in report.issues if i.level == "нарушение"]
        advice = [i for i in report.issues if i.level != "нарушение"]
        lines = [f"Проверка оформления «{filename}» по ГОСТ Р 7.0.97-2016: нарушений — {len(errors)}, "
                 f"рекомендаций — {len(advice)}."]
        if errors:
            lines += ["\n**Нарушения:**"] + [f"- {i.text} {ref(i.clause)}" for i in errors]
        if advice:
            lines += ["\n**Рекомендации:**"] + [f"- {i.text} {ref(i.clause)}" for i in advice]
        if report.passed:
            lines.append(f"\n**Соблюдено:** {', '.join(report.passed)}.")
        if not report.issues:
            lines.insert(1, "\nЗамечаний к оформлению нет.")
        lines.append("\nПроверены параметры оформления: формат и поля, шрифт, абзацный отступ, интервалы, выравнивание, "
                     "нумерация страниц, заголовок к тексту, даты. Реквизиты и содержание проверьте вручную.")
        result = AgentResult(asked, role, "normo", "answer", "\n".join(lines), sources=sources,
                             stats={"normocontrol": len(report.issues)})
        result.seconds = round(time.time() - t0, 2)
        self._log(result, user)
        return result

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
        if RECORD_RE.search(question) or STOCK_RE.search(question):
            return "database"    # номер записи или «где лежит / на складе» без вопроса о правиле — учёт
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
        if not 5 <= len(out) <= 300 or out.lower().rstrip("?. ") == question.lower().rstrip("?. "):
            # модель не переписала уточнение — ищем по предыдущему вопросу вместе с ним:
            # «Что делать при отказе оборудования? А если это случилось ночью?»
            return f"{history[-1]['question'].rstrip()} {question}"
        return out

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

    def shorten(self, question: str) -> str:
        """Длинный вопрос с приветствием и рассказом о себе — к сути: иначе поиск тонет в лишних словах."""
        try:
            out, _ = llm.chat([{"role": "user", "content": SHORTEN_PROMPT.format(question=question)}], max_tokens=80)
        except llm.ModelError:
            return question
        out = out.strip().splitlines()[0].strip(" «»\"") if out.strip() else ""
        records = {r.upper() for r in RECORD_RE.findall(question)}
        if not 8 <= len(out) <= 250 or records - {r.upper() for r in RECORD_RE.findall(out)}:
            return question          # сжатие потеряло номер записи — ищем по исходному вопросу
        return out

    @staticmethod
    def is_vague(question: str, retrieval) -> bool:
        """Короткий вопрос без предмета, а найденное разбросано по разным документам."""
        docs = {h.chunk.doc_id for h in retrieval.hits if not h.neighbor}
        return (len(tokenize(question)) <= 2 and len(docs) >= 3
                and settings.rerank_threshold <= retrieval.best_score < settings.confident_threshold)

    # ---------- справка ----------

    def small_talk(self, question: str, role: str, allowed: set[str]) -> "AgentResult | None":
        """Приветствие, «что ты умеешь», «откуда ответы», «спасибо», пустой ввод — справка с примерами."""
        docs = sum(1 for c in self.cards.values() if c["access"] in allowed)
        faq = [(PROMPT_Q_RE, PROMPT_TEXT, EXAMPLES), (WRITE_Q_RE, WRITE_TEXT, DB_EXAMPLES),
               (CREATIVE_Q_RE, CREATIVE_TEXT, EXAMPLES), (MODEL_Q_RE, MODEL_TEXT, None),
               (OFFLINE_Q_RE, OFFLINE_TEXT, None), (ERRORS_Q_RE, ERRORS_TEXT, None),
               (PRIVACY_Q_RE, PRIVACY_TEXT, None), (OPINION_Q_RE, OPINION_TEXT, EXAMPLES)]
        for rx, text, options in faq:
            if rx.search(question):
                return AgentResult(question, role, "help", "info", text, options=list(options or []))
        if GREETING_RE.match(question):
            text = (f"Здравствуйте! Я отвечаю на вопросы по регламентам, инструкциям, ГОСТам и данным учёта "
                    f"предприятия — со ссылкой на документ, пункт и страницу. Вам доступно документов: {docs}. "
                    "Например:")
        elif HELP_RE.search(question):
            text = ("Я ищу ответы во внутренних документах предприятия и в учётной базе:\n\n"
                    "- правила, сроки, порядок действий, требования — по регламентам, инструкциям, ГОСТам, "
                    "руководствам к приборам;\n"
                    "- оборудование, средства измерений, склад и заявки на ремонт — по учётной базе;\n"
                    "- вопрос вроде «прибор СИ-0012 просрочен — можно ли им работать?» — сразу по записи учёта "
                    "и требованию документа;\n"
                    "- какие документы есть по теме и о чём документ.\n\n"
                    f"Каждый ответ — со ссылкой на источник. Вам доступно документов: {docs}. Например:")
        elif ABOUT_RE.search(question):
            text = ABOUT_TEXT
        elif THANKS_RE.match(question):
            return AgentResult(question, role, "help", "info", "Пожалуйста! Если появятся вопросы — спрашивайте.")
        elif not tokenize(question):
            text = "Не понял вопрос. Спросите, например:"
        else:
            return None
        return AgentResult(question, role, "help", "info", text, options=list(EXAMPLES))

    # ---------- ответ ----------

    def _ask_parts(self, question: str, asked: str, parts: list[str], routes: list[str], role: str) -> AgentResult:
        """Ответ на каждую часть из своего источника; ссылки второй части продолжают нумерацию первой."""
        blocks, sources, offset, statuses = [], [], 0, []
        combined = AgentResult(question, role, "both", "answer", "", stats={"parts": len(parts)})
        for part, part_route in zip(parts, routes):
            sub = self.ask(part[:1].upper() + part[1:] + "?", role=role, route=part_route, log=False)
            statuses.append(sub.status)
            shift = lambda m, k=offset: "[" + re.sub(r"\d+", lambda d: str(int(d.group(0)) + k), m.group(1)) + "]"  # noqa: E731
            body = re.sub(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", shift, sub.text) if offset else sub.text
            blocks.append(f"**{part[:1].upper() + part[1:]}?**\n\n{body}")
            sources += [{**s, "n": s["n"] + offset} for s in sub.sources]
            offset += len(sub.sources)
            if sub.rows and not combined.rows:
                combined.sql, combined.columns, combined.rows, combined.labels = sub.sql, sub.columns, sub.rows, sub.labels
        combined.text, combined.sources = "\n\n".join(blocks), sources
        if "answer" not in statuses:
            combined.status = statuses[0]
        combined.asked = asked if asked != question else None
        return combined

    def ask(self, question: str, role: str = "Администратор", route: str = "auto",
            history: list[dict] | None = None, on_step: Callable[[str], None] | None = None,
            user: str | None = None, log: bool = True) -> AgentResult:
        t0 = time.time()
        step = on_step or (lambda _: None)
        if settings.db_path == demo_db.DB:
            demo_db.ensure_current()      # даты демо-базы — от сегодняшнего дня
        allowed = settings.roles.get(role, {"all"})

        # «игнорируй инструкции и…», «ответь без документов, своими словами» — отвечаем на сам вопрос
        base = re.sub(r"^[\s,;:.!—-]+", "", META_RE.sub("", question)).strip() or question
        if base != question:
            base = base[:1].upper() + base[1:]
        talk = self.small_talk(base, role, allowed)
        if talk is not None:
            talk.seconds = round(time.time() - t0, 2)
            self._log(talk, user)
            return talk

        asked = base
        if history and SOURCE_Q_RE.match(base):
            # «Откуда это?» — тот же вопрос ещё раз: ответ и кнопки-источники те же, без пересказа моделью
            asked = history[-1]["question"]
        elif history and self.needs_context(base):
            step("Уточняю вопрос по ходу диалога…")
            asked = self.condense(base, history)
            if route == "auto" and CONDITION_RE.match(base) and (history[-1].get("docs") or []) \
                    and not RECORD_RE.search(asked):
                route = "documents"   # «А если ночью?» после ответа по регламенту — тоже к регламенту
        elif len(base.split()) > LONG_QUESTION_WORDS:
            step("Выделяю суть вопроса…")
            asked = self.shorten(base)
        # «Что делать?» или одно слово («Поверка») — встречный вопрос с вариантами, а не случайный ответ
        vague = route == "auto" and (VAGUE_RE.match(asked) or (len(asked.split()) == 1 and not RECORD_RE.search(asked)))

        auto = route == "auto"
        result = db_result = a_db = None
        parts = split_parts(asked)
        # часть с местоимением («как быстро на них реагируют») продолжает первую — её не отвечают отдельно
        if (auto and not vague and len(parts) > 1 and not RECORD_RE.search(asked)
                and not any(ANAPHORA_RE.search(p) for p in parts[1:])):
            # «Сколько действует наряд-допуск и где лежит флюс?» — одна часть к документам, другая к учёту
            routes = [self.route(p) for p in parts]
            if "database" in routes and set(routes) != {"database"}:
                step("Вопрос из двух частей — отвечаю на каждую…")
                result = self._ask_parts(question, asked, parts, routes, role)
                result.seconds = round(time.time() - t0, 2)
                if log:
                    self._log(result, user)
                return result
        if route in ("auto", "documents") and EDITION_Q_RE.search(asked):
            step("Сравниваю редакции документа…")
            result = self.answer_editions(asked, role, allowed)
        if result is not None:
            route = "documents"
        elif route in ("auto", "documents") and DOCS_Q_RE.search(asked):
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
            route = "documents" if vague else self.route(asked)

        if route in ("database", "both"):
            step("Запрос к учётной базе…")
            # номер записи в вопросе — запись ищется по номеру программно, без генерации SQL
            records = [m.upper() for m in RECORD_RE.findall(asked)]
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
            # вопрос называет документ («по ГОСТ 7.0.97», «у MAS830L») — сначала ищем в нём:
            # иначе «поля документа» находятся в другом стандарте
            docs = named_docs(asked, self.cards)
            retrieval = retrieve(self.index, asked, allowed, docs or None)
            if docs and not retrieval.confident:
                retrieval = retrieve(self.index, asked, allowed)
            if summary:
                # и с состоянием записи: «партия П-2609-04» сама по себе не похожа на «ЭРИ, не прошедшие
                # входной контроль», а «ЭРИ, карантин» — похожа; но длинное описание записи может перебить
                # сам вопрос («кому звонить ночью»), поэтому найденное по обоим запросам объединяется
                merge(retrieval, retrieve(self.index, f"{asked} {summary}", allowed), limit=3)
            if asked != base and history:
                # уточнение в диалоге: ищем и вместе с темой предыдущего вопроса («отказ категории Б» +
                # «ночная смена» → раздел о ночной смене), добавляем до двух новых фрагментов
                extra = retrieve(self.index, f"{asked} {history[-1]['question']}", allowed)
                seen = {h.chunk.id for h in retrieval.hits}
                retrieval.hits += [h for h in extra.hits if not h.neighbor and h.chunk.id not in seen][:2]
            if len(parts) > 1:
                # составной вопрос («чем отличаются категории и как быстро реагируют») — ищем по каждой части,
                # иначе поиск находит пункты только про одну из них
                for part in parts:
                    merge(retrieval, retrieve(self.index, part, allowed), limit=2)
            prev_docs = set((history or [{}])[-1].get("docs") or [])
            if (asked != base and history and not retrieval.confident and retrieval.hits
                    and retrieval.hits[0].chunk.doc_id in prev_docs
                    and retrieval.best_score >= settings.rerank_threshold / 3):
                # уточнение в диалоге, и лучший фрагмент — из документа предыдущего ответа: отвечаем,
                # но как в «зоне сомнения» — с проверкой, что ответ о том же предмете
                retrieval.confident = True
            if auto and route == "documents" and (vague or self.is_vague(asked, retrieval)):
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
                a = answer_from_documents(asked, retrieval, facts=facts, parts=parts)
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
                elif auto and db_result is None and a.status == "no_data" and not RULE_RE.search(asked):
                    # вопрос о правиле («за сколько дней уведомляет») в учёте не ищем: записи — не ответ
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
        if log:
            self._log(result, user)
        return result

    @staticmethod
    def _log(r: AgentResult, user: str | None) -> None:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "role": r.role, "question": mask(r.question),
                  "asked": mask(r.asked), "route": r.route, "status": r.status, "answer": mask(r.text), "seconds": r.seconds,
                  "cited": [s["label"] for s in r.sources if s["cited"]], "sql": r.sql,
                  # ответ по документу, загруженному и ещё не утверждённому, — пробел в документации не закрывает
                  "unapproved": any(s.get("doc_kind") == "Загруженный документ" for s in r.sources if s["cited"])}
        with open(settings.log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

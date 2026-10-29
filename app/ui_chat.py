"""Страница чата: диалоги, ответы со ссылками, окно источника, оценки."""
import re
import time

import pandas as pd
import streamlit as st

from app.ui_common import (APP_NAME, BADGES, LOGO, ROUTE_LABELS, USER_AVATAR, ctx, file_bytes, get_agent,
                           health, inject_css, page_png)
from kbagent import auth, changes, effect, experts, prajs, spravochnik, tkp, tkp_registry, voice
from kbagent.config import settings
from kbagent.database import column_label, display
from kbagent.actions import log_action
from kbagent.export import answer_docx, draft_docx, draft_text
from kbagent.preview import source_path
from kbagent.ingest import expired_on, registry_info
from kbagent.text import md_plain
from kbagent.uploads import KIND_PENDING

# примеры на пустом экране — по аудитории: запросы клиентов о приборах, производство, метрология
TKP_EXAMPLE = ("Подготовь ТКП по письму клиента: «Просим предложить 2 осциллографа с полосой не менее 150 МГц "
               "и 4 каналами, мультиметр для измерения тока до 15 А — 3 шт., паяльную станцию для пайки при "
               "450 °C и источник питания 30 В»")
CLIENT_EXAMPLES = [
    "Клиенту нужен осциллограф с полосой не менее 150 МГц и 4 каналами — что предложить?",
    "Можно ли мультиметром MAS830L измерить ток 15 А?",
    "Подойдёт ли паяльная станция SBK936B для пайки при 450 °C?",
    "Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)?",
    "Какая частота дискретизации у АКИП-4115/1В?",
    "Сколько стоит поверка одного осциллографа?",
]
EXAMPLES = {
    "Инженер производства": [
        "Что делать при искрении оборудования?",
        "Пришла партия из 250 резисторов. Сколько штук осмотреть внешне и сколько проверить по электрическим "
        "параметрам?",
        "Можно ли выдать в производство партию П-2609-04?",
        "Что изменилось в новой редакции СТП ДП-02?",
        "Можно ли мультиметром MAS830L измерить ток 15 А?",
        "На какой срок выдаётся наряд-допуск?",
    ],
    "Метролог": [
        "У каких средств измерений истёк срок поверки?",
        "Какая частота дискретизации у АКИП-4115/1В?",
        "Какая полоса пропускания у АКИП-4115/2В?",
        "Что делать при искрении оборудования?",
        "Сколько стоит поверка одного осциллографа?",
        "Что изменилось в новой редакции СТП ДП-02?",
    ],
}
ROUTES = {"Автоматически": "auto", "Только документы": "documents", "Только учётная база": "database"}

CHAT_CSS = """
<style>
[data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] { max-width: 980px; }
[class*="st-key-sources"] button { padding: .2rem .7rem; min-height: 0; justify-content: flex-start; text-align: left; }
[class*="st-key-sources"] button > div { justify-content: flex-start; }
[class*="st-key-sources"] button p { font-size: .84rem; }
[class*="st-key-conv-"] button, [class*="st-key-conv-"] button * { justify-content: flex-start; text-align: left; }
[class*="st-key-conv-"] button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: .88rem; }
/* телефон: без аватаров — ответу вся ширина экрана */
@media (max-width: 640px) {
  [data-testid^="stChatMessageAvatar"] { display: none; }
  [data-testid="stChatMessage"] { gap: 0; padding-left: .2rem; padding-right: .2rem; }
}
</style>
"""


# ---------- диалоги ----------

def _key() -> str:
    return ctx()["history_key"]


def _conv() -> dict:
    if "conv" not in st.session_state:
        convs = auth.list_conversations(_key())
        st.session_state.conv = (convs and auth.load_conversation(_key(), convs[0]["id"])) or auth.new_conversation()
    return st.session_state.conv


def _save() -> None:
    if _conv()["items"]:
        auth.save_conversation(_key(), _conv())


def sidebar() -> str:
    conv = _conv()
    with st.sidebar:
        if st.button("Новый диалог", icon=":material/add_comment:", type="primary", width="stretch"):
            st.session_state.conv = auth.new_conversation()
            st.rerun()
        convs = auth.list_conversations(_key())
        if convs:
            query = st.text_input("Поиск по диалогам", placeholder="Поиск по диалогам", key="conv_search",
                                  label_visibility="collapsed")
            shown = [c for c in convs if not query or query.lower() in (c["title"] + " " + c["questions"]).lower()]
            if not shown:
                st.caption("Ничего не найдено")
            # длинный список прокручивается сам: имя пользователя и «Выйти» остаются на экране ноутбука
            box = st.container(height=min(49 * len(shown[:40]) + 4, 200), border=False) if shown else st.container()
            for c in shown[:40]:
                active = c["id"] == conv["id"]
                left, right = box.columns([7, 1], vertical_alignment="center", gap="small")
                if left.button(c["title"], key=f"conv-{c['id']}", type="secondary" if active else "tertiary",
                               width="stretch", help=c["updated"]):
                    st.session_state.conv = auth.load_conversation(_key(), c["id"]) or auth.new_conversation()
                    st.rerun()
                if right.button(":material/delete:", key=f"del-{c['id']}", type="tertiary", help="Удалить диалог"):
                    auth.delete_conversation(_key(), c["id"])
                    if active:
                        st.session_state.conv = auth.new_conversation()
                    st.rerun()
        with st.popover("Где искать ответ", icon=":material/tune:", width="stretch"):
            mode = st.radio("Где искать ответ", list(ROUTES), key="route_mode", label_visibility="collapsed",
                            help="Обычно агент сам выбирает: документы, учётная база или оба источника")
            if ctx()["is_admin"]:
                st.toggle("Режим показа", key="show_mode",
                          help="Скрыть служебные блоки администратора (найденные фрагменты, запрос к базе) — "
                               "для показа и записи экрана")
    return ROUTES[mode]


def _debug() -> bool:
    """Служебные блоки — администратору, кроме режима показа."""
    return ctx()["is_admin"] and not st.session_state.get("show_mode")


# ---------- ответ ----------

def _short(label: str, limit: int = 95) -> str:
    """Длинное название документа сокращается, а пункт, страница, версия и срок действия остаются."""
    if len(label) <= limit:
        return label
    cut = min((label.find(m) for m in (", п. ", ", стр. ", ", с. ", " · ") if m in label), default=-1)
    if cut < 0:
        return label[:limit - 1].rstrip() + "…"
    title, rest = label[:cut], label[cut:]
    keep = limit - len(rest) - 1
    return f"{title[:max(keep, 30)].rstrip()}…{rest}"


@st.dialog("Источник", width="large")
def show_source(s: dict, key: str) -> None:
    st.markdown(f"**{s['label']}**")
    path = source_path(s["doc_id"])
    is_pdf = bool(path and path.suffix.lower() == ".pdf" and s.get("page"))
    if is_pdf:
        # фрагмент, перешедший на следующую страницу, показывается с обеих страниц
        last = min(s.get("page_end") or s["page"], s["page"] + 2)
        shots = [(page, *page_png(str(path), page, s["text"], crop=True)) for page in range(s["page"], last + 1)]
        best = max(found for _, _, found in shots)
        hits = [(page, png) for page, png, found in shots if found and found >= max(3, best // 5)]
        for page, png in hits:
            st.image(png, caption=f"Страница {page} — фрагмент выделен", width="stretch")
        if st.toggle("Показать страницу целиком", key=f"whole-{key}") or not hits:
            for page in [page for page, _ in hits] or [s["page"]]:
                png, _ = page_png(str(path), page, s["text"])
                st.image(png, caption=f"Страница {page} файла «{path.name}»", width="stretch")
    elif path and path.suffix.lower() == ".docx":
        st.caption("В документе Word нет страниц — ссылка указывает раздел и пункт.")
    with st.expander("Текст фрагмента", expanded=not is_pdf):
        st.write(s["text"])
    if path:
        st.download_button("Скачать документ", data=file_bytes(str(path)), file_name=path.name,
                           key=f"dl-{key}", icon=":material/download:")


def _unique(labels: list[str]) -> list[str]:
    seen, out = {}, []
    for lab in labels:
        seen[lab] = seen.get(lab, 0) + 1
        out.append(lab if seen[lab] == 1 else f"{lab} ({seen[lab]})")
    return out


def _on_feedback(i: int, key: str) -> None:
    item = _conv()["items"][i]
    item["feedback"] = st.session_state.get(key)
    if item["feedback"] is not None:
        auth.save_feedback({"user": ctx()["login"], "role": item["role"], "question": item["question"],
                            "answer": item["text"], "status": item["status"],
                            "score": "👍" if item["feedback"] == 1 else "👎"})
        _save()


def _approve_draft(i: int, key: str) -> None:
    item = _conv()["items"][i]
    item["draft"] = st.session_state.get(key) or item.get("draft", "")
    item["draft_approved"] = {"by": ctx()["name"], "at": time.strftime("%d.%m.%Y %H:%M")}
    cited = [s for s in item.get("sources", []) if s.get("cited")]
    kind = "ТКП" if item.get("tkp") else "ответ"
    if not item.get("effect_id"):          # повторное скачивание того же черновика — не новый результат
        item["effect_id"] = effect.record(kind, ctx()["login"], item["role"], item["question"],
                                          time.time() - item.get("ts", time.time()), cited,
                                          len((item.get("tkp") or {}).get("positions", [])))
    log_action("tkp_approved" if item.get("tkp") else "draft_approved", ctx()["login"], item["question"][:120],
               f"источников: {len(cited)}")
    _save()


@st.dialog("Черновик ответа", width="large")
def draft_dialog(i: int, uid: str) -> None:
    """Черновик ответа клиенту или коллеге. Автоматической отправки нет: сотрудник проверяет источники,
    правит текст и утверждает — утверждение записывается в журнал действий."""
    _draft_body(i, uid)


@st.dialog("Черновик ТКП", width="large")
def tkp_dialog(i: int, uid: str) -> None:
    """ТКП по запросу клиента: сопроводительное письмо правится здесь, таблица позиций — из справочника."""
    _draft_body(i, uid)


def _tkp_choice(i: int, uid: str, data: dict) -> dict:
    """Менеджер выбирает прибор по позиции из подходящих по требованиям (первым — минимально достаточный).
    Разбор письма повторяется программой с этим выбором — характеристики, источники, цена и сумма пересчитываются."""
    options = [p for p in data["positions"] if len(p.get("fit_models") or []) > 1]
    if not options or not data.get("letter"):
        return data                          # ТКП из старых диалогов — без выбора
    prices, _ = prajs.load()
    st.markdown("**Прибор по позициям** — из подходящих по всем требованиям; первым — минимально достаточный")
    chosen = {}
    for p in options:
        models = p["fit_models"]

        def label(m: str, first: str = models[0]) -> str:
            return (m + (f" — {prajs.money(prices[m].price)}" if m in prices else " — цены нет в прайсе")
                    + (" · минимально достаточный" if m == first else ""))
        chosen[p["n"]] = st.selectbox(f"{p['n']}. {p['kind']} — {p['qty']} шт.", models, format_func=label,
                                      index=models.index(p["model"]) if p["model"] in models else 0,
                                      key=f"tkp-pick-{uid}-{p['n']}")
    if all(chosen[p["n"]] == p["model"] for p in options):
        return data
    item = _conv()["items"][i]
    fresh = tkp.parse(data["letter"], settings.roles.get(item["role"], {"all"}), chosen=chosen)
    if fresh is None:
        return data
    new = fresh.to_dict()
    new["expert_ids"] = data.get("expert_ids", [])
    item["tkp"] = new
    item.pop("draft", None)                  # письмо клиенту собирается заново под выбранные приборы
    _save()
    return new


def _tkp_approved(item: dict) -> None:
    """Утверждённое ТКП: номер, статус и те самые Word и Excel, что сохранены в реестре."""
    number = item["tkp_number"]
    record = next((r for r in tkp_registry.load() if r["number"] == number), {})
    d = record.get("date", "")
    when = f"{d[8:10]}.{d[5:7]}.{d[:4]} {d[11:16]}" if len(d) >= 16 else d
    st.success(f"ТКП № {number} утверждён: {record.get('approved_by', '—')}, {when}. "
               f"Статус — {record.get('status', '—')}; сменить — на странице «Реестр ТКП».")
    docx_path, xlsx_path = tkp_registry.files(number)
    c1, c2 = st.columns(2)
    if docx_path.exists():
        c1.download_button("Скачать ТКП в Word", docx_path.read_bytes(), file_name=docx_path.name, type="primary",
                           icon=":material/description:", width="stretch", key=f"tkp-dl-{number}",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if xlsx_path.exists():
        c2.download_button("Скачать в Excel", xlsx_path.read_bytes(), file_name=xlsx_path.name,
                           icon=":material/table:", width="stretch", key=f"tkp-xl-{number}",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _approve_tkp(i: int, key: str, data: dict) -> None:
    """Утверждение ТКП: номер из реестра, Word и Excel на бланке компании, «Эффект» и журнал — как у черновика."""
    item = _conv()["items"][i]
    record = tkp_registry.approve(data, st.session_state.get(key) or "", ctx()["login"], ctx()["name"],
                                  item["role"], item.get("asked") or item["question"], APP_NAME,
                                  revision_of=item.get("tkp_revision_of"))
    item["tkp_number"] = record["number"]
    _approve_draft(i, key)


def _draft_body(i: int, uid: str) -> None:
    item = _conv()["items"][i]
    data = item.get("tkp")
    if data and item.get("tkp_number"):
        _tkp_approved(item)
        return
    if data:
        if item.get("tkp_revision_of"):
            st.info(f"Новая редакция ТКП № {item['tkp_revision_of']}: при утверждении получит номер "
                    f"{item['tkp_revision_of']}/1 (или следующий), прежняя версия в реестре станет «заменён».",
                    icon=":material/history:")
        data = _tkp_choice(i, uid, data)
        st.caption("Сопроводительное письмо к ТКП. Позиции, характеристики и соответствие требованиям — в таблице "
                   "документа Word, они взяты из справочника параметров со ссылками на руководства; цены и сроки "
                   "впишите в Word. Ничего не отправляется автоматически.")
    else:
        st.caption("Проверьте текст и источники — ссылки под ответом в чате открывают страницу с выделенным "
                   "фрагментом. Ничего не отправляется автоматически.")
    key = f"draft-{uid}"
    if data:                                 # другой прибор — новое поле: браузер не оставит прежнее письмо
        key += "-" + "-".join(p.get("model") or "" for p in data["positions"])
    default = tkp.draft_text(data, ctx()["name"]) if data else draft_text(item, ctx()["name"])
    text = st.text_area("Текст", value=item.get("draft") or default, height=360, key=key,
                        label_visibility="collapsed")
    if data:
        st.caption("При утверждении ТКП получает номер в реестре и собирается на бланке компании — в Word и Excel.")
        # утверждение — в обработчике: при следующей отрисовке окно покажет номер и файлы ТКП
        st.button("Утвердить ТКП", type="primary", icon=":material/task_alt:", key=f"tkp-ok-{uid}",
                  on_click=_approve_tkp, args=(i, key, data))
        return
    when = time.strftime("%d.%m.%Y %H:%M")
    doc = draft_docx(text, item, ctx()["name"], when, APP_NAME)
    st.download_button("Утвердить и скачать Word", data=doc, file_name=f"chernovik-{i + 1}.docx", type="primary",
                       icon=":material/task_alt:",
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       on_click=_approve_draft, args=(i, key), key=f"draft-ok-{uid}")


def _send_expert(i: int, key: str) -> None:
    item = _conv()["items"][i]
    comment = (st.session_state.get(key) or "").strip()
    cited = [s for s in item.get("sources", []) if s.get("cited")]
    answer = re.sub(r"\s+", " ", item["text"].replace("**", "")).strip()
    details = f"Ответ агента: {answer[:700]}{'…' if len(answer) > 700 else ''}" + (
        " Источники: " + "; ".join(f"[{s['n']}] {s['label']}" for s in cited) if cited else " Источников нет.")
    it = experts.add("передал сотрудник", item.get("asked") or item["question"], details, item["question"],
                     ctx()["login"], item["role"], comment)
    if it["id"] not in item.setdefault("expert_ids", []):
        item["expert_ids"].append(it["id"])
    log_action("expert_sent", ctx()["login"], item["question"][:120], comment[:200])
    _save()


@st.dialog("Передать эксперту")
def expert_dialog(i: int, uid: str) -> None:
    """Сотрудник сомневается в ответе — вопрос, ответ агента и источники уходят в очередь эксперта.
    Решение эксперта появится под ответом в чате."""
    st.caption("Вопрос, ответ агента и источники попадут в очередь «Вопросы эксперту» (страница «Администрирование»). "
               "Решение эксперта появится под этим ответом.")
    key = f"exp-{uid}"
    st.text_area("Что вызывает сомнение (необязательно)", key=key, height=110,
                 placeholder="Например: в руководстве и в справочнике разные значения; клиент спрашивает "
                             "о модификации, которой нет в документах")
    if st.button("Передать", type="primary", icon=":material/forward_to_inbox:", key=f"exp-ok-{uid}"):
        _send_expert(i, key)
        st.rerun()


def _on_comment(i: int, key: str) -> None:
    item = _conv()["items"][i]
    text = (st.session_state.get(key) or "").strip()
    if text:
        item["comment"] = text
        auth.save_feedback({"user": ctx()["login"], "role": item["role"], "question": item["question"],
                            "answer": item["text"], "status": item["status"], "score": "👎", "comment": text})
        _save()


def render_answer(item: dict, i: int) -> None:
    uid = f"{_conv()['id']}-{i}"
    kind = item["route"] if item["status"] == "answer" else item["status"]
    label, color, icon = BADGES.get(kind, BADGES["error"])
    st.badge(label, icon=icon, color=color)
    if item.get("asked"):
        st.caption(f"Понял вопрос как: «{item['asked']}»")
    st.markdown(re.sub(r"(\[\d+(?:\s*[,–-]\s*\d+)*\])", r"**\1**", item["text"]).replace("[БД]", "**[БД]**"))

    # варианты уточнения нажимаются один раз: после выбора кнопки исчезают (и не «нажимаются»
    # повторно, когда браузер переподключается к перезапущенному серверу)
    if item.get("options") and not item.get("options_used"):
        for n, option in enumerate(item["options"]):
            if st.button(option, key=f"opt-{uid}-{n}", icon=":material/arrow_forward:"):
                item["options_used"] = True
                _save()
                st.session_state.pending = option
                st.rerun()

    cited = [s for s in item.get("sources", []) if s["cited"]]
    expired = {s["doc_id"]: expired_on(registry_info(s["doc_id"])) for s in cited}
    expired = {d: day for d, day in expired.items() if day}
    if expired:
        names = "; ".join(f"«{registry_info(d).get('title', d)}» — до {day[8:10]}.{day[5:7]}.{day[:4]}"
                          for d, day in expired.items())
        st.warning(f"Срок действия документа истёк: {names}. Проверьте, какая редакция действует сейчас, "
                   "и сообщите администратору базы знаний.", icon=":material/event_busy:")
    if reasons := changes.stale(item):
        st.warning("После этого ответа изменился источник: " + "; ".join(reasons) + ". Переспросите — ответ будет "
                   "по действующей редакции.", icon=":material/update:")
        if item["route"] in ("documents", "both"):
            st.button("Переспросить", key=f"reask-{uid}", icon=":material/refresh:", on_click=_ask,
                      args=(item["question"],))
    if any(s.get("doc_kind") == KIND_PENDING for s in cited):
        st.warning("Ответ опирается на документ, загруженный пользователем и ещё не утверждённый.",
                   icon=":material/pending:")

    if item.get("rows"):
        labels = [lab[:1].upper() + lab[1:] for lab in item.get("labels") or [column_label(c) for c in item["columns"]]]
        rows = [[display(v) if isinstance(v, str) else v for v in r] for r in item["rows"]]   # даты — 13.11.2026
        df = pd.DataFrame(rows, columns=_unique(labels))
        if len(df) == 1 and len(df.columns) > 4:
            # одна запись уже изложена в ответе — карточка «поле — значение» свёрнута, чтобы не повторять текст
            df = pd.DataFrame({"Поле": df.columns, "Значение": [str(v) for v in df.iloc[0]]})
            with st.expander("Запись в учёте — все поля", icon=":material/table_rows:"):
                st.dataframe(df, hide_index=True, width="stretch")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
        if _debug() and item.get("sql"):
            with st.expander("Запрос к учётной базе"):
                st.code(item["sql"], language="sql")

    base = item.get("baseline")
    if base and base.get("docs") and item["status"] == "answer":
        pages = f", {base['pages']} стр." if base.get("pages") else ""
        with st.expander(f"Обычный поиск выдал бы: документов — {len(base['docs'])}{pages}; ответ пришлось бы "
                         "искать и сверять вручную", icon=":material/manage_search:"):
            st.caption("Поиск по словам без агента — список документов, а не ответ:")
            for d in base["docs"]:
                st.caption(f"- {d['title']}" + (f" — {d['pages']} стр." if d.get("pages") else ""))

    if cited:
        with st.container(key=f"sources-{uid}"):
            st.caption("Источники — нажмите, чтобы открыть фрагмент в документе")
            for s in cited:
                if st.button(f"[{s['n']}] {_short(s['label'])}", key=f"src-{uid}-{s['n']}",
                             icon=":material/description:", width="stretch"):
                    show_source(s, f"{uid}-{s['n']}")
    other = [s for s in item.get("sources", []) if not s["cited"]]
    if _debug() and other and item["status"] == "answer":
        with st.expander("Другие найденные фрагменты (видно администратору)"):
            for s in other:
                st.caption(f"[{s['n']}] {s['label']} — релевантность {s['score']}")
    elif other and item["status"] in ("no_data", "model_refused"):
        hints, seen = [], set()
        for s in other:
            if s["score"] >= 0.05 and s.get("doc_id") not in seen:
                seen.add(s.get("doc_id"))
                hints.append(s)
        if hints:
            st.caption("Возможно, поможет: " + "; ".join(_short(s["label"], 70) for s in hints[:3]))

    left, mid, draft, expert, right = st.columns([3, 2, 2, 2, 2], vertical_alignment="center")
    seconds = f"{item['seconds']:.1f}".replace(".", ",")
    left.caption(ROUTE_LABELS.get(item["route"], "") + ("" if item["status"] == "info" else f" · {seconds} с"))
    with mid:
        if item["status"] not in ("error", "info"):       # справку («что ты умеешь») не оцениваем
            if item.get("feedback") is None:
                st.feedback("thumbs", key=f"fb-{uid}", on_change=_on_feedback, args=(i, f"fb-{uid}"))
            else:
                st.caption("Оценка: " + ("👍" if item["feedback"] == 1 else "👎"))
    if item["status"] == "answer" and draft.button(
            "Черновик ТКП" if item.get("tkp") else "Черновик", key=f"dr-{uid}", icon=":material/edit_note:",
            type="tertiary", help="Черновик ответа клиенту или коллеге (по запросу клиента — ТКП в Word): "
                                  "правка текста и утверждение сотрудником"):
        (tkp_dialog if item.get("tkp") else draft_dialog)(i, uid)
    if item["status"] not in ("error", "info") and expert.button(
            "Эксперту", key=f"xp-{uid}", icon=":material/support_agent:", type="tertiary",
            help="Сомневаетесь в ответе или источники расходятся — передать вопрос эксперту"):
        expert_dialog(i, uid)
    if item.get("draft_approved"):
        kind = f"ТКП № {item['tkp_number']}" if item.get("tkp_number") else "ТКП" if item.get("tkp") else "Черновик"
        st.caption(f"{kind} утверждён: {item['draft_approved']['by']}, {item['draft_approved']['at']}")
        stale = next((r for r in effect.affected() if r["id"] == item.get("effect_id")), None) \
            if item.get("effect_id") else None
        if stale:
            st.warning("После утверждения изменились источники — перепроверьте ответ клиенту: "
                       + "; ".join(stale["reasons"]), icon=":material/sync_problem:")
    for eid in item.get("expert_ids", []):
        it = experts.get(eid)
        if it and it["status"] == "решён":
            st.success(f"**Ответ эксперта** ({it.get('resolved_by') or '—'}, {display(it.get('resolved_at', ''))[:16]}): "
                       f"{it.get('note') or 'вопрос решён'}", icon=":material/verified:")
        elif it:
            st.caption(f"Вопрос передан эксперту: {it['subject']} — решение появится здесь")
    if item["status"] != "info":
        right.download_button("В Word", data=answer_docx(item, APP_NAME, ctx()["name"]), file_name=f"otvet-{i + 1}.docx",
                              mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              key=f"md-{uid}", icon=":material/download:", type="tertiary",
                              help="Ответ со ссылками на источники — документом Word")
    if item.get("feedback") == 0 and not item.get("comment"):
        st.text_input("Что не так с ответом? (необязательно)", key=f"cm-{uid}", on_change=_on_comment,
                      args=(i, f"cm-{uid}"))


# ---------- страница ----------

def _hero() -> None:
    st.image(LOGO, width=52)
    st.markdown(f"### {APP_NAME}")
    st.markdown("Отвечаю по документации, справочнику параметров продукции и данным учёта — со ссылкой "
                "на документ, пункт и страницу. Подбираю приборы по требованиям клиента, по письму клиента готовлю "
                "черновик ТКП — утверждаете вы. Если ответа в источниках нет, так и скажу.")
    st.caption("Скрепка в поле ввода: письмо клиента (Word, PDF, текст, .eml) — подготовлю черновик ТКП; приказ "
               "или служебная записка в Word — проверю оформление по ГОСТ Р 7.0.97."
               + (" Микрофон — вопрос голосом: речь распознаётся на этом компьютере, запись не сохраняется."
                  if voice.available() else ""))
    if ctx()["role"] not in EXAMPLES:          # запросы клиентов: главный сценарий — письмо → ТКП
        if st.button("Пример: письмо клиента → черновик ТКП", icon=":material/request_quote:", type="primary"):
            st.session_state.pending = TKP_EXAMPLE
            st.rerun()
    st.caption("Например:")
    cols = st.columns(2)
    for k, q in enumerate(EXAMPLES.get(ctx()["role"], CLIENT_EXAMPLES)):
        if cols[k % 2].button(q, key=f"ex-{k}", width="stretch"):
            st.session_state.pending = q
            st.rerun()


def result_item(question: str, r, role: str) -> dict:
    """Ответ агента — запись диалога (её показывает чат и сохраняет история)."""
    return {"question": question, "asked": r.asked, "role": role, "route": r.route,
            "status": r.status, "text": r.text, "sources": r.sources, "sql": r.sql, "columns": r.columns,
            "labels": r.labels, "rows": r.rows, "seconds": r.seconds, "options": r.options,
            "expert_ids": list(r.stats.get("expert_ids", [])), "tkp": r.stats.get("tkp"),
            "as_of": r.stats.get("as_of"),
            "baseline": (get_agent().search_baseline(r.asked or question, role)
                         if r.status == "answer" and r.route not in ("help", "normo") else None),
            "ts": time.time(), "at": time.strftime("%d.%m.%Y %H:%M")}


def open_in_chat(question: str, r, role: str, **extra) -> None:
    """Новый диалог с готовым ответом — например, разбор письма из почты или новая редакция ТКП из реестра
    (extra: tkp_revision_of — номер ТКП, которое заменит эта редакция); чат откроется на нём."""
    conv = auth.new_conversation()
    item = result_item(question, r, role)
    item.update(extra)
    conv["items"].append(item)
    auth.save_conversation(_key(), conv)
    st.session_state.conv = conv


CHANGE_ICONS = {"edition": ":material/published_with_changes:", "added": ":material/note_add:",
                "updated": ":material/edit_document:", "removed": ":material/delete:",
                "effective": ":material/event_available:", "expired": ":material/event_busy:"}
CLAUSE_KINDS = {"changed": "изменён", "added": "добавлен", "removed": "исключён"}


def _ask(question: str, seen: bool = False) -> None:
    if seen:
        changes.mark_seen(ctx()["login"])
    st.session_state.pending = question


def _change(e: dict) -> None:
    text = f"{CHANGE_ICONS[e['kind']]} **{changes.KINDS[e['kind']]}:** «{e['title']}»"
    if e.get("old_title"):
        text += f" вместо «{e['old_title']}»"
    st.markdown(text + (f" — {e['summary']}" if e.get("summary") else "") + f"  \n:gray[{display(e['time'])[:10]}]")
    if e["kind"] == "edition":
        st.button("Что изменилось", key=f"chg-{e['id']}", icon=":material/difference:", on_click=_ask,
                  args=(f"Что изменилось в {e['title']}?", True))
    elif e["kind"] in ("added", "effective"):
        st.button("О чём документ", key=f"chg-{e['id']}", icon=":material/description:", on_click=_ask,
                  args=(f"О чём документ «{e['title']}»?", True))
    elif e.get("changes"):
        with st.expander("Что изменилось"):
            for c in e["changes"]:
                st.markdown(f"**п. {c['number']}** — {CLAUSE_KINDS[c['kind']]}: {c['text']}")


def notices() -> None:
    """Изменения документов, доступных роли, с прошлого раза — сотрудник узнаёт о новой редакции, не спрашивая."""
    events = changes.feed(ctx()["login"], ctx()["role"])
    if not events:
        return
    convs = [c for c in (auth.load_conversation(_key(), x["id"]) for x in auth.list_conversations(_key())[:30]) if c]
    affected = sum(changes.affected_answers(convs, events).values())
    with st.container(border=True, key="doc-changes"):
        st.markdown(f"**Изменения в документах** · {len(events)}")
        for e in events[:3]:
            _change(e)
        if len(events) > 3:
            with st.expander(f"Ещё изменений: {len(events) - 3}"):
                for e in events[3:]:
                    _change(e)
        if affected:
            st.caption(f"Ответов в ваших диалогах по прежним версиям: {affected} — они помечены, их стоит переспросить.")
        st.button("Понятно", key="chg-seen", icon=":material/done_all:", on_click=changes.mark_seen,
                  args=(ctx()["login"],))


def chat_page() -> None:
    inject_css(CHAT_CSS)
    if st.query_params.get("show") == "1":      # ссылка для записи экрана: сразу в режиме показа
        st.session_state.show_mode = True
    route = sidebar()
    conv = _conv()
    items = conv["items"]

    sent = st.chat_input("Спросите о продукции, документах или учёте; вставьте или приложите письмо клиента — "
                         "подготовлю ТКП", accept_file=True, file_type=tkp.LETTER_TYPES,
                         accept_audio=voice.available(), audio_sample_rate=16000)
    attached = sent.files[0] if sent and sent.files else None
    question = (sent.text if sent else None) or st.session_state.pop("pending", None)
    spoken = getattr(sent, "audio", None) if sent and not (sent.text or "").strip() and not attached else None
    if spoken is not None:
        # голосовой вопрос: распознаётся здесь же; обозначения моделей — как в справочнике параметров
        try:
            with st.spinner("Распознаю вопрос…"):
                question = voice.transcribe(spoken.getvalue(), {v.model for v in spravochnik.load()[0]})
        except voice.VoiceError as e:
            st.warning(f"Не получилось распознать: {e}.", icon=":material/mic_off:")
            question = None
        if spoken is not None and question == "":
            st.warning("Вопрос не расслышан — повторите ближе к микрофону или напишите его.", icon=":material/mic_off:")
            question = None
    typed = (question or "").strip()
    if attached is not None:
        question = f"{typed or 'Приложенный файл'} · {attached.name}"
    if question and items and items[-1]["question"] == question and time.time() - items[-1].get("ts", 0) < 10:
        question = None          # двойное нажатие — тот же вопрос только что задан

    notices()
    if not items and not question:
        _hero()
    for i, item in enumerate(items):
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(md_plain(item["question"]))
            if item.get("voice"):
                st.caption(":material/mic: голосом")
        with st.chat_message("assistant", avatar=LOGO):
            render_answer(item, i)

    if not question:
        return
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(md_plain(question))
        if spoken is not None:
            st.caption(":material/mic: голосом — так я понял вопрос")
    with st.chat_message("assistant", avatar=LOGO):
        if attached is None and not health()["llm"]:      # нормоконтролю и ТКП модель не нужна
            st.error("Сервис ответов временно недоступен. Попробуйте через минуту или сообщите администратору.",
                     icon=":material/cloud_off:")
            return
        context = [{"question": h.get("asked") or h["question"], "text": h["text"],
                    "docs": [s["doc_id"] for s in h.get("sources", []) if s["cited"]]} for h in items[-3:]]
        holder = st.empty()
        with holder.status("Читаю приложенный файл…" if attached is not None else "Ищу ответ…") as status:
            if attached is not None:
                r = get_agent().attachment(attached.name, attached.getvalue(), role=ctx()["role"], question=typed,
                                           user=ctx()["login"])
                question = r.question if " · " in r.question else question
            else:
                r = get_agent().ask(question, role=ctx()["role"], route=route, history=context or None,
                                    user=ctx()["login"], on_step=lambda msg: status.update(label=msg))
        holder.empty()                 # этапы нужны, пока идёт ответ; после — не загромождают диалог
        item = result_item(question, r, ctx()["role"])
        if spoken is not None:
            item["voice"] = True
        items.append(item)
        _save()
        if len(items) == 1:
            st.rerun()             # первый вопрос нового диалога — сразу показать диалог в списке слева
        render_answer(item, len(items) - 1)

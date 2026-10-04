"""Страница чата: диалоги, ответы со ссылками, окно источника, оценки."""
import re
import time

import pandas as pd
import streamlit as st

from app.ui_common import (APP_NAME, BADGES, LOGO, ROUTE_LABELS, USER_AVATAR, ctx, file_bytes, get_agent,
                           health, inject_css, page_png)
from kbagent import auth
from kbagent.database import column_label, display
from kbagent.preview import source_path
from kbagent.uploads import KIND_PENDING

EXAMPLES = [
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск?",
    "Можно ли выдать в производство партию П-2609-04?",
    "У каких средств измерений истёк срок поверки?",
    "Какие документы есть про паяльные станции?",
    "Сколько стоит поверка одного осциллографа?",
]
ROUTES = {"Автоматически": "auto", "Только документы": "documents", "Только учётная база": "database"}

CHAT_CSS = """
<style>
[data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] { max-width: 980px; }
[class*="st-key-sources"] button { padding: .2rem .7rem; min-height: 0; justify-content: flex-start; text-align: left; }
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
        if st.button("Новый диалог", icon=":material/add_comment:", type="primary", use_container_width=True):
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
                               use_container_width=True, help=c["updated"]):
                    st.session_state.conv = auth.load_conversation(_key(), c["id"]) or auth.new_conversation()
                    st.rerun()
                if right.button(":material/delete:", key=f"del-{c['id']}", type="tertiary", help="Удалить диалог"):
                    auth.delete_conversation(_key(), c["id"])
                    if active:
                        st.session_state.conv = auth.new_conversation()
                    st.rerun()
        with st.popover("Где искать ответ", icon=":material/tune:", use_container_width=True):
            mode = st.radio("Где искать ответ", list(ROUTES), key="route_mode", label_visibility="collapsed",
                            help="Обычно агент сам выбирает: документы, учётная база или оба источника")
    return ROUTES[mode]


# ---------- ответ ----------

def _short(label: str, limit: int = 95) -> str:
    if len(label) <= limit:
        return label
    title, _, rest = label.partition(", п. ") if ", п. " in label else label.partition(", стр. ")
    keep = limit - len(rest) - 6
    return f"{title[:max(keep, 30)].rstrip()}…" + (f", {'п. ' if ', п. ' in label else 'стр. '}{rest}" if rest else "")


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
            st.image(png, caption=f"Страница {page} — фрагмент выделен", use_container_width=True)
        if st.toggle("Показать страницу целиком", key=f"whole-{key}") or not hits:
            for page in [page for page, _ in hits] or [s["page"]]:
                png, _ = page_png(str(path), page, s["text"])
                st.image(png, caption=f"Страница {page} файла «{path.name}»", use_container_width=True)
    elif path and path.suffix.lower() == ".docx":
        st.caption("В документе Word нет страниц — ссылка указывает раздел и пункт.")
    with st.expander("Текст фрагмента", expanded=not is_pdf):
        st.write(s["text"])
    if path:
        st.download_button("Скачать документ", data=file_bytes(str(path)), file_name=path.name,
                           key=f"dl-{key}", icon=":material/download:")


def _answer_md(item: dict) -> str:
    lines = [f"# Вопрос\n\n{item['question']}\n", f"# Ответ\n\n{item['text']}\n"]
    cited = [s for s in item.get("sources", []) if s["cited"]]
    if cited:
        lines.append("# Источники\n\n" + "\n".join(f"{s['n']}. {s['label']}" for s in cited) + "\n")
    lines.append(f"_{APP_NAME}, {item.get('at', time.strftime('%d.%m.%Y %H:%M'))}_\n")
    return "\n".join(lines)


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
                st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.dataframe(df, hide_index=True, use_container_width=True)
        if ctx()["is_admin"] and item.get("sql"):
            with st.expander("Запрос к учётной базе"):
                st.code(item["sql"], language="sql")

    if cited:
        with st.container(key=f"sources-{uid}"):
            st.caption("Источники — нажмите, чтобы открыть фрагмент в документе")
            for s in cited:
                if st.button(f"[{s['n']}] {_short(s['label'])}", key=f"src-{uid}-{s['n']}",
                             icon=":material/description:", use_container_width=True):
                    show_source(s, f"{uid}-{s['n']}")
    other = [s for s in item.get("sources", []) if not s["cited"]]
    if ctx()["is_admin"] and other and item["status"] == "answer":
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

    left, mid, right = st.columns([5, 2, 2], vertical_alignment="center")
    seconds = f"{item['seconds']:.1f}".replace(".", ",")
    left.caption(ROUTE_LABELS.get(item["route"], "") + ("" if item["status"] == "info" else f" · {seconds} с"))
    with mid:
        if item["status"] not in ("error", "info"):       # справку («что ты умеешь») не оцениваем
            if item.get("feedback") is None:
                st.feedback("thumbs", key=f"fb-{uid}", on_change=_on_feedback, args=(i, f"fb-{uid}"))
            else:
                st.caption("Оценка: " + ("👍" if item["feedback"] == 1 else "👎"))
    if item["status"] != "info":
        right.download_button("Скачать", data=_answer_md(item), file_name=f"otvet-{i + 1}.md", mime="text/markdown",
                              key=f"md-{uid}", icon=":material/download:", type="tertiary")
    if item.get("feedback") == 0 and not item.get("comment"):
        st.text_input("Что не так с ответом? (необязательно)", key=f"cm-{uid}", on_change=_on_comment,
                      args=(i, f"cm-{uid}"))


# ---------- страница ----------

def _hero() -> None:
    st.image(LOGO, width=52)
    st.markdown(f"### {APP_NAME}")
    st.markdown("Отвечаю по регламентам, инструкциям, ГОСТам и данным учёта предприятия — со ссылкой "
                "на документ, пункт и страницу. Если ответа в источниках нет, так и скажу.")
    st.caption("Например:")
    cols = st.columns(2)
    for k, q in enumerate(EXAMPLES):
        if cols[k % 2].button(q, key=f"ex-{k}", use_container_width=True):
            st.session_state.pending = q
            st.rerun()


def chat_page() -> None:
    inject_css(CHAT_CSS)
    route = sidebar()
    conv = _conv()
    items = conv["items"]

    question = st.chat_input("Спросите о регламентах, инструкциях или данных учёта")
    question = question or st.session_state.pop("pending", None)
    if question and items and items[-1]["question"] == question and time.time() - items[-1].get("ts", 0) < 10:
        question = None          # двойное нажатие — тот же вопрос только что задан

    if not items and not question:
        _hero()
    for i, item in enumerate(items):
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(item["question"])
        with st.chat_message("assistant", avatar=LOGO):
            render_answer(item, i)

    if not question:
        return
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)
    with st.chat_message("assistant", avatar=LOGO):
        if not health()["llm"]:
            st.error("Сервис ответов временно недоступен. Попробуйте через минуту или сообщите администратору.",
                     icon=":material/cloud_off:")
            return
        context = [{"question": h.get("asked") or h["question"], "text": h["text"],
                    "docs": [s["doc_id"] for s in h.get("sources", []) if s["cited"]]} for h in items[-3:]]
        holder = st.empty()
        with holder.status("Ищу ответ…") as status:
            r = get_agent().ask(question, role=ctx()["role"], route=route, history=context or None,
                                user=ctx()["login"], on_step=lambda msg: status.update(label=msg))
        holder.empty()                 # этапы нужны, пока идёт ответ; после — не загромождают диалог
        item = {"question": question, "asked": r.asked, "role": ctx()["role"], "route": r.route,
                "status": r.status, "text": r.text, "sources": r.sources, "sql": r.sql, "columns": r.columns,
                "labels": r.labels, "rows": r.rows, "seconds": r.seconds, "options": r.options,
                "ts": time.time(), "at": time.strftime("%d.%m.%Y %H:%M")}
        items.append(item)
        _save()
        if len(items) == 1:
            st.rerun()             # первый вопрос нового диалога — сразу показать диалог в списке слева
        render_answer(item, len(items) - 1)

"""Чат с агентом по документам и учётной базе предприятия.

    streamlit run app/streamlit_app.py

Вход по логину и паролю (демо-учётки — scripts/make_users.py); KB_AUTH=0 — выбор роли из списка.
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import auth, llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.ingest import ocr_available  # noqa: E402
from kbagent.preview import render_page, source_path  # noqa: E402
from kbagent.uploads import (KIND_PENDING, add_upload, approve_upload, documents,  # noqa: E402
                             list_uploads, remove_upload)

DEMO_QUESTIONS = [
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск на работы в электроустановках и можно ли его продлить?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "У каких средств измерений истёк срок поверки?",
    "Можно ли выдать в производство партию П-2609-04?",
    "Сколько стоит поверка одного осциллографа в аккредитованной организации?",
]

STATUS_VIEW = {
    "answer": ("success", "Ответ со ссылками на пункты документов — при необходимости сверьтесь с источником"),
    "no_data": ("info", "Ответа в доступных документах нет — агент не выдумывает"),
    "model_refused": ("info", "В найденных фрагментах ответа нет — агент не выдумывает"),
    "unverified": ("warning", "Ответ не подтверждён ссылкой и не показан"),
    "clarify": ("info", "Нужно уточнение"),
    "error": ("error", "Ошибка"),
}
ROUTE_VIEW = {"database": "Ответ по записям учётной базы — запрос и таблица ниже",
              "both": "Ответ по учётной базе и документам — со ссылками на записи и пункты"}
ACCESS_LABELS = {"all": "Все сотрудники", "production": "Производство", "metrology": "Метрология",
                 "admin": "Только администратор"}
COOKIE = "kb_session"

st.set_page_config(page_title="Агент по документам предприятия", page_icon="📄", layout="wide")


@st.cache_resource(show_spinner="Загружаю индекс документов…")
def get_agent() -> Agent:
    return Agent()


@st.cache_data(show_spinner=False, max_entries=200)
def page_png(path: str, page: int, fragment: str) -> tuple[bytes, int]:
    return render_page(path, page, fragment)


@st.cache_data(show_spinner=False, max_entries=50)
def file_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def index_meta() -> dict:
    path = settings.index_dir / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def set_cookie(value: str, max_age: int) -> None:
    """Сеанс переживает перезагрузку страницы: в браузере хранится только случайный токен."""
    components.html(f"<script>parent.document.cookie = '{COOKIE}={value}; path=/; max-age={max_age}; "
                    "SameSite=Strict';</script>", height=0)


# ---------- вход ----------

def current_user() -> dict | None:
    if not settings.auth_required:
        return None
    if "user" in st.session_state:
        return st.session_state.user
    if not st.session_state.get("logged_out"):
        token = st.context.cookies.get(COOKIE)
        user = auth.session_user(token)
        if user:
            st.session_state.update(user=user, token=token)
            return user
    return None


def login_page() -> None:
    if st.session_state.pop("clear_cookie", False):
        set_cookie("", 0)
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("## Агент по документам и учёту")
        st.caption("Отвечает по внутренним документам и данным учёта предприятия — со ссылкой на документ, "
                   "пункт и страницу. Доступные документы зависят от роли сотрудника.")
        with st.form("login"):
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            submitted = st.form_submit_button("Войти", type="primary", use_container_width=True)
        if submitted:
            user = auth.authenticate(login, password)
            if user:
                token = auth.create_session(user)
                st.session_state.update(user=user, token=token, set_cookie=token, logged_out=False)
                st.session_state.pop("history", None)
                st.rerun()
            st.error("Неверный логин или пароль")
        st.caption("Демо-учётные записи (инженер, метролог, администратор) — в файле scripts/make_users.py.")
    st.stop()


user = current_user()
if settings.auth_required and user is None:
    login_page()
if token := st.session_state.pop("set_cookie", None):
    set_cookie(token, auth.SESSION_TTL)


# ---------- ответ ----------

def render_source(s: dict, key: str) -> None:
    """Фрагмент, страница оригинала с выделением и кнопка скачивания документа."""
    st.write(s["text"])
    path = source_path(s["doc_id"])
    if path is None:
        return
    if path.suffix.lower() == ".pdf" and s.get("page"):
        last = min(s.get("page_end") or s["page"], s["page"] + 1)
        for page in range(s["page"], last + 1):
            png, found = page_png(str(path), page, s["text"])
            if found or page == s["page"]:
                st.image(png, caption=f"Страница {page} файла «{path.name}» — фрагмент выделен", use_container_width=True)
    elif path.suffix.lower() == ".docx":
        st.caption("Документ Word: у него нет страниц, ссылка указывает раздел и пункт.")
    st.download_button("Скачать документ", data=file_bytes(str(path)), file_name=path.name, key=key)


def on_feedback(i: int, key: str) -> None:
    item = st.session_state.history[i]
    item["feedback"] = st.session_state.get(key)
    if item["feedback"] is None:
        return
    auth.save_feedback({"user": login_name, "role": item["role"], "question": item["question"],
                        "answer": item["text"], "status": item["status"],
                        "score": "👍" if item["feedback"] == 1 else "👎"})
    auth.save_history(history_key, st.session_state.history)


def on_comment(i: int, key: str) -> None:
    item = st.session_state.history[i]
    text = (st.session_state.get(key) or "").strip()
    if text:
        item["comment"] = text
        auth.save_feedback({"user": login_name, "role": item["role"], "question": item["question"],
                            "answer": item["text"], "status": item["status"], "score": "👎", "comment": text})
        auth.save_history(history_key, st.session_state.history)


def render_answer(r: dict, i: int) -> None:
    if r["status"] == "answer" and r["route"] in ROUTE_VIEW:
        st.success(ROUTE_VIEW[r["route"]])
    else:
        kind, label = STATUS_VIEW.get(r["status"], ("info", r["status"]))
        getattr(st, kind)(label)
    if r.get("asked"):
        st.caption(f"Понял вопрос как: «{r['asked']}»")
    text = re.sub(r"(\[\d+(?:\s*[,–-]\s*\d+)*\])", r"**\1**", r["text"]).replace("[БД]", "**[БД]**")
    st.markdown(text)

    for n, option in enumerate(r.get("options", [])):
        if st.button(option, key=f"opt-{i}-{n}"):
            st.session_state.pending = option
            st.rerun()

    cited = [s for s in r.get("sources", []) if s["cited"]]
    other = [s for s in r.get("sources", []) if not s["cited"]]
    if any(s.get("doc_kind") == KIND_PENDING for s in cited):
        st.warning("Ответ опирается на документ, загруженный пользователем и ещё не утверждённый администратором.")

    if r.get("sql"):
        with st.expander("Источник: учётная база — записи" + (" и запрос" if is_admin else ""), expanded=False):
            if is_admin:
                st.code(r["sql"], language="sql")
            if r["rows"]:
                st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, use_container_width=True)
            else:
                st.caption("Записей не найдено.")

    if cited:
        st.markdown("**Источники**")
        for s in cited:
            with st.expander(f"[{s['n']}] {s['label']}"):
                render_source(s, key=f"dl-{i}-{s['n']}")
    if other and r["status"] not in ("answer", "clarify"):
        # только заметно похожие фрагменты и по одному на документ — иначе подсказка выглядит случайной
        hints, seen = [], set()
        for s in other:
            if s["score"] >= 0.05 and s.get("doc_id") not in seen:
                seen.add(s.get("doc_id"))
                hints.append(s)
        if hints:
            st.markdown("**Где ещё можно поискать**")
            for s in hints[:3]:
                st.caption(f"• {s['label']}")
    elif other and r["status"] == "answer":
        with st.expander("Другие найденные фрагменты"):
            for s in other:
                st.caption(f"[{s['n']}] {s['label']} — релевантность {s['score']}")
                st.write(s["text"][:400] + ("…" if len(s["text"]) > 400 else ""))

    source = {"database": "учётная база", "both": "учётная база и документы"}.get(r["route"], "документы")
    st.caption(f"Источник: {source} · время ответа {r['seconds']} с · роль: {r['role']}")

    if r["status"] in ("answer", "no_data", "model_refused", "clarify"):
        if r.get("feedback") is None:
            st.feedback("thumbs", key=f"fb-{i}", on_change=on_feedback, args=(i, f"fb-{i}"))
        else:
            st.caption("Ваша оценка: " + ("👍" if r["feedback"] == 1 else "👎"))
            if r["feedback"] == 0 and not r.get("comment"):
                st.text_input("Что не так с ответом? (необязательно)", key=f"cm-{i}", on_change=on_comment,
                              args=(i, f"cm-{i}"))


# ---------- боковая панель ----------

with st.sidebar:
    if settings.auth_required:
        role = user["role"]
        login_name, history_key = user["login"], user["login"]
        st.markdown(f"**{user['name']}**  \nРоль: {role}")
        if st.button("Выйти", use_container_width=True):
            auth.end_session(st.session_state.get("token"))
            for k in ("user", "token", "history"):
                st.session_state.pop(k, None)
            st.session_state.update(logged_out=True, clear_cookie=True)
            st.rerun()
    else:
        role = st.selectbox("Роль сотрудника", list(settings.roles), index=len(settings.roles) - 1,
                            help="Роль определяет, какие документы доступны агенту")
        login_name, history_key = None, "_demo"
    is_admin = role == "Администратор"

    mode = st.radio("Где искать", ["Автоматически", "Только документы", "Только учётная база"])
    route = {"Автоматически": "auto", "Только документы": "documents", "Только учётная база": "database"}[mode]

    st.divider()
    st.subheader("Демо-вопросы")
    for q in DEMO_QUESTIONS:
        if st.button(q, use_container_width=True, key=f"demo-side-{q}"):
            st.session_state.pending = q

    if is_admin:
        st.divider()
        st.subheader("Добавить документ")
        agent = get_agent()
        st.caption("PDF или DOCX. До утверждения документ помечается «не утверждён»."
                   + ("" if ocr_available() else " Сканы без текста не распознаются: python scripts/setup.py --ocr"))
        access = st.selectbox("Кому доступен", list(ACCESS_LABELS), format_func=ACCESS_LABELS.get)
        active = {d["id"]: d["title"] for d in documents(agent.index) if d["status"] == "действует"}
        replaces = st.selectbox("Это новая редакция документа…", [None] + list(active),
                                format_func=lambda x: "— не заменяет другой документ —" if x is None else active[x],
                                help="Старая редакция будет отменена и перестанет участвовать в поиске")
        st.session_state.setdefault("upload_key", 0)
        files = st.file_uploader("Файлы", type=["pdf", "docx"], accept_multiple_files=True,
                                 key=f"uploader-{st.session_state.upload_key}", label_visibility="collapsed")
        if files:
            notices = st.session_state.setdefault("notices", [])
            for f in files:
                bar = st.progress(0.0, text=f"Читаю «{f.name}»…")
                try:
                    res = add_upload(agent.index, f.name, f.getvalue(), access=access, user=login_name,
                                     replaces=replaces,
                                     progress=lambda done, total, b=bar, name=f.name:
                                     b.progress(done / total, text=f"«{name}»: {done} из {total} фрагментов"))
                    if res["chunks"]:
                        notices.append(("success", f"«{res['title']}» добавлен: {res['chunks']} фрагментов"))
                    else:
                        notices.append(("warning", f"В «{res['title']}» не найден текст" +
                                        (" — скан: установите распознавание (python scripts/setup.py --ocr)"
                                         if res.get("scanned_pages") else "")))
                    if res.get("ocr_pages"):
                        notices.append(("info", f"Распознано страниц-сканов: {res['ocr_pages']} — проверьте ответы по ним"))
                    if res["replaced_same"]:
                        notices.append(("warning", f"Заменён ранее загруженный документ с тем же именем «{res['title']}»"))
                    if replaces:
                        notices.append(("info", f"Прежняя редакция «{active[replaces]}» отменена"))
                except Exception as e:  # noqa: BLE001 — показать администратору причину
                    notices.append(("error", f"«{f.name}»: {e}"))
                bar.empty()
            st.session_state.upload_key += 1   # очищаем поле загрузки
            st.rerun()
        for kind, msg in st.session_state.pop("notices", []):
            getattr(st, kind)(msg)
        for d in list_uploads():
            col1, col2, col3 = st.columns([5, 1, 1])
            mark = "✅" if d.get("approved") else "⏳ не утверждён"
            col1.caption(f"{d['title']} · {ACCESS_LABELS.get(d.get('access'), d.get('access'))} · {mark}")
            if not d.get("approved") and col2.button("✓", key=f"ok-{d['id']}", help="Утвердить документ"):
                approve_upload(agent.index, d["id"], user=login_name)
                st.rerun()
            if col3.button("✕", key=f"rm-{d['id']}", help="Удалить документ из базы знаний"):
                remove_upload(agent.index, d["id"])
                st.rerun()

    st.divider()
    st.subheader("Состояние")
    health = llm.health()
    names = {"llm": "Языковая модель GigaChat 3.1 Lightning", "embed": "Поиск по смыслу (bge-m3)",
             "rerank": "Переранжировщик (bge-reranker-v2-m3)"}
    for key, name in names.items():
        st.markdown(f"{'🟢' if health[key] else '🔴'} {name}")
    meta = index_meta()
    if meta:
        st.caption(f"Индекс: {meta['documents']} документов, {meta['chunks']} фрагментов, собран {meta['built']}")
    st.caption("Все модели работают локально, без доступа в интернет.")
    if st.button("Очистить диалог", use_container_width=True):
        st.session_state.history = []
        auth.save_history(history_key, [])


# ---------- основная часть ----------

st.markdown("### Агент по документам и учёту")
st.caption("Отвечает только по внутренним документам и данным учёта — со ссылкой на документ, пункт и страницу. "
           "Если ответа нет, честно говорит об этом.")

if "history" not in st.session_state:
    st.session_state.history = auth.load_history(history_key)
history = st.session_state.history

# поле ввода вне вкладок — Streamlit закрепляет его внизу экрана
question = st.chat_input("Задайте вопрос, например: что делать при отказе оборудования ночью?")
question = question or st.session_state.pop("pending", None)

tabs = st.tabs(["Чат", "Журнал запросов", "Оценки", "Документы", "Качество"]) if is_admin else [st.container()]

with tabs[0]:
    if not history and not question:
        st.info("Задайте вопрос внизу страницы или выберите пример.")
        for q in DEMO_QUESTIONS:
            if st.button(q, key=f"demo-main-{q}"):
                st.session_state.pending = q
                st.rerun()
    for i, item in enumerate(history):
        with st.chat_message("user"):
            st.write(item["question"])
        with st.chat_message("assistant"):
            render_answer(item, i)

    if question:
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            if not health["llm"]:
                st.error("Языковая модель не запущена. Запустите scripts\\start_servers.bat.")
            else:
                context = [{"question": h.get("asked") or h["question"], "text": h["text"],
                            "docs": [s["doc_id"] for s in h.get("sources", []) if s["cited"]]} for h in history[-3:]]
                with st.status("Ищу ответ…", expanded=True) as status:
                    r = get_agent().ask(question, role=role, route=route, history=context, user=login_name,
                                        on_step=lambda msg: status.update(label=msg))
                    status.update(label=f"Готово за {r.seconds} с", state="complete", expanded=False)
                item = {"question": question, "asked": r.asked, "role": role, "route": r.route,
                        "status": r.status, "text": r.text, "sources": r.sources, "sql": r.sql,
                        "columns": r.columns, "rows": r.rows, "seconds": r.seconds, "options": r.options}
                history.append(item)
                auth.save_history(history_key, history)
                render_answer(item, len(history) - 1)

if is_admin:
    with tabs[1]:
        st.caption("Каждый запрос записывается в журнал logs/audit.jsonl: кто, что спросил, что получил и по каким источникам.")
        log = settings.log_dir / "audit.jsonl"
        if log.exists():
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[-300:]]
            df = pd.DataFrame(records[::-1]).reindex(
                columns=["time", "user", "role", "question", "asked", "route", "status", "seconds"])
            df.columns = ["Время", "Пользователь", "Роль", "Вопрос", "Понят как", "Источник", "Статус", "Секунд"]
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("Журнал пока пуст.")

    with tabs[2]:
        feedback = auth.load_feedback()
        if feedback:
            df = pd.DataFrame(feedback[::-1]).reindex(
                columns=["time", "user", "score", "question", "answer", "comment", "status"])
            up, down = (df["score"] == "👍").sum(), (df["score"] == "👎").sum()
            c1, c2 = st.columns(2)
            c1.metric("Полезных ответов 👍", int(up))
            c2.metric("Неудачных ответов 👎", int(down))
            df.columns = ["Время", "Пользователь", "Оценка", "Вопрос", "Ответ", "Комментарий", "Статус"]
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.caption("Ответы с 👎 — кандидаты в эталонный набор вопросов (eval/questions.yaml).")
        else:
            st.info("Оценок пока нет: сотрудники ставят 👍/👎 под каждым ответом.")

    with tabs[3]:
        docs = documents(get_agent().index)
        df = pd.DataFrame(docs).reindex(columns=["title", "kind", "access", "status", "replaced_by"])
        df["access"] = df["access"].map(lambda a: ACCESS_LABELS.get(a, a))
        df.columns = ["Документ", "Вид", "Доступ", "Статус", "Заменён на"]
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.caption("Отменённые редакции хранятся для истории, но в поиске не участвуют.")

    with tabs[4]:
        results = ROOT / "eval" / "results" / "latest.md"
        if results.exists():
            st.markdown(results.read_text(encoding="utf-8"))
        else:
            st.info("Запустите python eval/run_eval.py, чтобы получить метрики качества.")

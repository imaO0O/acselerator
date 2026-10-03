"""Чат с агентом по документам и учётной базе предприятия.

    streamlit run app/streamlit_app.py
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.preview import render_page, source_path  # noqa: E402
from kbagent.uploads import add_upload, list_uploads, remove_upload  # noqa: E402

DEMO_QUESTIONS = [
    "Что делать при искрении оборудования?",
    "На какой срок выдаётся наряд-допуск на работы в электроустановках и можно ли его продлить?",
    "Какие поля должны быть у документа по ГОСТ Р 7.0.97-2016?",
    "У каких средств измерений истёк срок поверки?",
    "Сколько стоит поверка одного осциллографа в аккредитованной организации?",
]

STATUS_VIEW = {
    "answer": ("success", "Ответ подтверждён источниками"),
    "no_data": ("info", "Ответа в доступных документах нет — агент не выдумывает"),
    "model_refused": ("info", "В найденных фрагментах ответа нет — агент не выдумывает"),
    "unverified": ("warning", "Ответ не подтверждён ссылкой и не показан"),
    "error": ("error", "Ошибка"),
}

st.set_page_config(page_title="Агент по документам предприятия", layout="wide")


@st.cache_resource(show_spinner="Загружаю индекс документов…")
def get_agent() -> Agent:
    return Agent()


def index_meta() -> dict:
    path = settings.index_dir / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@st.cache_data(show_spinner=False, max_entries=200)
def page_png(path: str, page: int, fragment: str) -> tuple[bytes, int]:
    return render_page(path, page, fragment)


@st.cache_data(show_spinner=False, max_entries=50)
def file_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


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


def render_answer(r: dict, key: str = "x") -> None:
    kind, label = STATUS_VIEW.get(r["status"], ("info", r["status"]))
    getattr(st, kind)(label)
    text = re.sub(r"(\[\d+(?:\s*[,–-]\s*\d+)*\])", r"**\1**", r["text"]).replace("[БД]", "**[БД]**")
    st.markdown(text)

    if r["route"] == "database" and r.get("sql"):
        with st.expander("Источник: учётная база — запрос и результат", expanded=False):
            st.code(r["sql"], language="sql")
            if r["rows"]:
                st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, use_container_width=True)
            else:
                st.caption("Запрос вернул пустой результат.")

    cited = [s for s in r.get("sources", []) if s["cited"]]
    other = [s for s in r.get("sources", []) if not s["cited"]]
    if cited:
        st.markdown("**Источники**")
        for s in cited:
            with st.expander(f"[{s['n']}] {s['label']}"):
                render_source(s, key=f"dl-{key}-{s['n']}")
    if other and r["status"] != "answer":
        st.markdown("**Где ещё можно поискать**")
        for s in other[:3]:
            st.caption(f"• {s['label']}")
    elif other:
        with st.expander("Другие найденные фрагменты"):
            for s in other:
                st.caption(f"[{s['n']}] {s['label']} — релевантность {s['score']}")
                st.write(s["text"][:400] + ("…" if len(s["text"]) > 400 else ""))

    source = "учётная база" if r["route"] == "database" else "документы"
    st.caption(f"Источник: {source} · время ответа {r['seconds']} с · роль: {r['role']}")


# ---------- боковая панель ----------
with st.sidebar:
    st.header("Настройки")
    role = st.selectbox("Роль сотрудника", list(settings.roles), index=len(settings.roles) - 1,
                        help="Роль определяет, какие документы доступны агенту")
    mode = st.radio("Где искать", ["Автоматически", "Только документы", "Только учётная база"])
    route = {"Автоматически": "auto", "Только документы": "documents", "Только учётная база": "database"}[mode]

    st.divider()
    st.subheader("Демо-вопросы")
    for q in DEMO_QUESTIONS:
        if st.button(q, use_container_width=True):
            st.session_state.pending = q

    st.divider()
    st.subheader("Добавить документ")
    st.caption("PDF или DOCX — через минуту агент отвечает и по нему.")
    st.session_state.setdefault("upload_key", 0)
    files = st.file_uploader("Файлы", type=["pdf", "docx"], accept_multiple_files=True,
                             key=f"uploader-{st.session_state.upload_key}", label_visibility="collapsed")
    if files:
        agent = get_agent()
        for f in files:
            with st.spinner(f"Читаю и индексирую «{f.name}»…"):
                try:
                    _, title, n = add_upload(agent.index, f.name, f.getvalue())
                    st.session_state.setdefault("notices", []).append(
                        ("success", f"«{title}» добавлен: {n} фрагментов") if n else
                        ("warning", f"В «{title}» нет текстового слоя (скан) — нужен модуль распознавания"))
                except Exception as e:  # noqa: BLE001 — показать пользователю причину
                    st.session_state.setdefault("notices", []).append(("error", f"«{f.name}»: {e}"))
        st.session_state.upload_key += 1   # очищаем поле загрузки
        st.rerun()
    for kind, msg in st.session_state.pop("notices", []):
        getattr(st, kind)(msg)
    for d in list_uploads():
        col1, col2 = st.columns([4, 1])
        col1.caption(d["title"])
        if col2.button("✕", key=f"rm-{d['id']}", help="Удалить документ из индекса"):
            remove_upload(get_agent().index, d["id"])
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

# ---------- основная часть ----------
st.title("Агент по документам и учётной базе предприятия")
st.caption("Отвечает только по внутренним документам и данным учёта — со ссылкой на документ, пункт и страницу. "
           "Если ответа нет, честно говорит об этом.")

st.session_state.setdefault("history", [])
# поле ввода вне вкладок — Streamlit закрепляет его внизу экрана
question = st.chat_input("Задайте вопрос, например: что делать при отказе оборудования ночью?")
question = question or st.session_state.pop("pending", None)
if question:
    if not health["llm"]:
        st.error("Языковая модель не запущена. Запустите scripts\\start_servers.bat.")
    else:
        with st.spinner(f"Ищу ответ: «{question}»"):
            r = get_agent().ask(question, role=role, route=route)
        st.session_state.history.append(
            {"question": question, "role": role, "route": r.route, "status": r.status, "text": r.text,
             "sources": r.sources, "sql": r.sql, "columns": r.columns, "rows": r.rows, "seconds": r.seconds})

chat_tab, log_tab, about_tab = st.tabs(["Чат", "Журнал запросов", "Качество"])

with chat_tab:
    if not st.session_state.history:
        st.info("Задайте вопрос внизу страницы или выберите демо-вопрос слева.")
    for i, item in enumerate(st.session_state.history):
        with st.chat_message("user"):
            st.write(item["question"])
        with st.chat_message("assistant"):
            render_answer(item, key=str(i))

with log_tab:
    st.caption("Каждый запрос записывается в журнал logs/audit.jsonl: кто, что спросил, что получил и по каким источникам.")
    log = settings.log_dir / "audit.jsonl"
    if log.exists():
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[-200:]]
        df = pd.DataFrame(records[::-1])[["time", "role", "question", "route", "status", "seconds"]]
        df.columns = ["Время", "Роль", "Вопрос", "Источник", "Статус", "Секунд"]
        st.dataframe(df, hide_index=True, use_container_width=True)
    else:
        st.info("Журнал пока пуст.")

with about_tab:
    results = ROOT / "eval" / "results" / "latest.md"
    if results.exists():
        st.markdown(results.read_text(encoding="utf-8"))
    else:
        st.info("Запустите python eval/run_eval.py, чтобы получить метрики качества.")

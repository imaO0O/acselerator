"""Страница администрирования: журнал, оценки, документы, качество, состояние системы."""
import json

import pandas as pd
import streamlit as st

from app.ui_common import (ACCESS_LABELS, ROOT, ROUTE_LABELS, STATUS_LABELS, ctx, get_agent, health, index_meta,
                           inject_css)
from kbagent import auth
from kbagent.config import settings
from kbagent.ingest import ocr_available
from kbagent.uploads import add_upload, approve_upload, documents, list_uploads, remove_upload

EVAL_USERS = {None, "", "eval"}


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def journal() -> None:
    log = settings.log_dir / "audit.jsonl"
    if not log.exists():
        st.info("Журнал пока пуст.")
        return
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[-5000:] if line.strip()]
    df = pd.DataFrame(records).reindex(columns=["time", "user", "role", "question", "asked", "route", "status",
                                               "seconds", "cited", "answer"])
    show_eval = st.toggle("Показывать прогоны эталона и служебные запросы", value=False,
                          help="Запросы скриптов проверки качества и запросы без входа в систему")
    if not show_eval:
        df = df[~df["user"].isin(EVAL_USERS) & df["user"].notna()]
    c1, c2, c3 = st.columns([2, 2, 3])
    users = c1.multiselect("Пользователи", sorted(df["user"].dropna().unique()), placeholder="Все")
    statuses = c2.multiselect("Итог", list(STATUS_LABELS.values()), placeholder="Любой")
    text = c3.text_input("Поиск по вопросу", placeholder="Например: поверка")
    df["status"] = df["status"].map(lambda s: STATUS_LABELS.get(s, s))
    df["route"] = df["route"].map(lambda s: ROUTE_LABELS.get(s, s))
    df["cited"] = df["cited"].map(lambda c: "; ".join(c) if isinstance(c, list) else c)
    if users:
        df = df[df["user"].isin(users)]
    if statuses:
        df = df[df["status"].isin(statuses)]
    if text:
        df = df[df["question"].str.contains(text, case=False, na=False)]
    df = df.iloc[::-1].fillna("—").replace("", "—")
    df.columns = ["Время", "Пользователь", "Роль", "Вопрос", "Понят как", "Источник", "Итог", "Секунд",
                  "Ссылки", "Ответ"]
    st.caption(f"Записей: {len(df)}. Журнал хранится в logs/audit.jsonl на сервере.")
    st.dataframe(df, hide_index=True, use_container_width=True, height=480,
                 column_config={"Ответ": st.column_config.TextColumn(width="large")})
    st.download_button("Выгрузить в CSV", _csv(df), file_name="zhurnal_zaprosov.csv", icon=":material/download:")


def feedback() -> None:
    records = auth.load_feedback()
    if not records:
        st.info("Оценок пока нет: сотрудники ставят 👍 или 👎 под каждым ответом.")
        return
    df = pd.DataFrame(records[::-1]).reindex(columns=["time", "user", "score", "question", "answer", "comment"])
    up, down = int((df["score"] == "👍").sum()), int((df["score"] == "👎").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Полезных ответов", up)
    c2.metric("Неудачных ответов", down)
    c3.metric("Доля полезных", f"{up / max(up + down, 1):.0%}")
    df = df.fillna("—").replace("", "—")
    df.columns = ["Время", "Пользователь", "Оценка", "Вопрос", "Ответ", "Комментарий"]
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption("Вопросы с 👎 — кандидаты в эталонный набор (eval/questions.yaml): после исправления "
               "они проверяются при каждом прогоне.")
    st.download_button("Выгрузить в CSV", _csv(df), file_name="otsenki.csv", icon=":material/download:")


def docs() -> None:
    agent = get_agent()
    with st.container(border=True):
        st.markdown("**Добавить документ**")
        st.caption("PDF или DOCX, в том числе сканы" + ("" if ocr_available() else
                   " (распознавание сканов не установлено)") + ". До утверждения документ помечается "
                   "«загружен пользователем, не утверждён».")
        all_docs = documents(agent.index)
        active = {d["id"]: d["title"] for d in all_docs if d["status"] == "действует"}
        c1, c2 = st.columns(2)
        access = c1.selectbox("Кому доступен", [k for k in ACCESS_LABELS if k != "archive"],
                              format_func=ACCESS_LABELS.get)
        replaces = c2.selectbox("Новая редакция документа", [None] + list(active),
                                format_func=lambda x: "— не заменяет другой документ —" if x is None else active[x],
                                help="Прежняя редакция будет отменена и перестанет участвовать в поиске")
        st.session_state.setdefault("upload_key", 0)
        files = st.file_uploader("Файлы", type=["pdf", "docx"], accept_multiple_files=True,
                                 key=f"uploader-{st.session_state.upload_key}", label_visibility="collapsed")
        if files:
            notices = st.session_state.setdefault("notices", [])
            for f in files:
                bar = st.progress(0.0, text=f"Читаю «{f.name}»…")
                try:
                    res = add_upload(agent.index, f.name, f.getvalue(), access=access, user=ctx()["login"],
                                     replaces=replaces,
                                     progress=lambda done, total, b=bar, name=f.name:
                                     b.progress(done / total, text=f"«{name}»: {done} из {total} фрагментов"))
                    notices.append(("success", f"«{res['title']}» добавлен: {res['chunks']} фрагментов")
                                   if res["chunks"] else ("warning", f"В «{res['title']}» не найден текст"))
                    if res.get("ocr_pages"):
                        notices.append(("info", f"Распознано страниц-сканов: {res['ocr_pages']}"))
                    if res["replaced_same"]:
                        notices.append(("warning", f"Заменён ранее загруженный «{res['title']}»"))
                    if replaces:
                        notices.append(("info", f"Прежняя редакция «{active[replaces]}» отменена"))
                except Exception as e:  # noqa: BLE001 — показать администратору причину
                    notices.append(("error", f"«{f.name}»: {e}"))
                bar.empty()
            st.session_state.upload_key += 1
            st.rerun()
        for kind, msg in st.session_state.pop("notices", []):
            getattr(st, kind)(msg)

    uploads = list_uploads()
    if uploads:
        st.markdown("**Загруженные документы**")
        for d in uploads:
            c1, c2, c3 = st.columns([6, 2, 1], vertical_alignment="center")
            state = "утверждён" if d.get("approved") else "ждёт утверждения"
            c1.markdown(f"{d['title']}  \n:gray[{ACCESS_LABELS.get(d.get('access'), d.get('access'))} · "
                        f"загрузил {d.get('uploaded_by') or '—'} {d.get('uploaded_at', '')} · {state}]")
            if not d.get("approved") and c2.button("Утвердить", key=f"ok-{d['id']}", icon=":material/check:"):
                approve_upload(agent.index, d["id"], user=ctx()["login"])
                st.rerun()
            if c3.button(":material/delete:", key=f"rm-{d['id']}", help="Удалить из базы знаний"):
                remove_upload(agent.index, d["id"])
                st.rerun()

    st.markdown("**Все документы базы знаний**")
    df = pd.DataFrame(documents(agent.index)).reindex(columns=["title", "kind", "access", "status", "replaced_by"])
    titles = {d["id"]: d["title"] for d in documents(agent.index)}
    df["access"] = df["access"].map(lambda a: ACCESS_LABELS.get(a, a))
    df["replaced_by"] = df["replaced_by"].map(lambda r: titles.get(r, r) if isinstance(r, str) else "—")
    df = df.fillna("—")
    df.columns = ["Документ", "Вид", "Доступ", "Статус", "Заменён на"]
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption("Отменённые редакции хранятся для истории, но в поиске не участвуют.")


def quality() -> None:
    results = ROOT / "eval" / "results"
    latest = results / "latest.md"
    if latest.exists():
        st.markdown(latest.read_text(encoding="utf-8"))
    else:
        st.info("Результатов проверки качества пока нет.")


def system() -> None:
    names = {"llm": "Языковая модель GigaChat 3.1 Lightning", "embed": "Поиск по смыслу (bge-m3)",
             "rerank": "Оценка релевантности (bge-reranker-v2-m3)"}
    state = health()
    for key, name in names.items():
        st.markdown(f"{':green[● работает]' if state[key] else ':red[● не отвечает]'} — {name}")
    if not all(state.values()):
        st.warning("Запустите серверы моделей: scripts\\start_servers.bat (на компьютере с видеокартой) "
                   "или scripts\\start_servers.bat -Cpu.")
    meta = index_meta()
    if meta:
        st.markdown(f"**База знаний:** {meta['documents']} документов, {meta['chunks']} фрагментов, "
                    f"обновлена {meta['built']}.")
    st.markdown("Все модели работают на этом компьютере, доступ в интернет не нужен.")
    for title, name in [("Скорость", "speed_DESKTOP-QMSBN1F.md"), ("Одновременные пользователи", "load.md"),
                        ("Масштаб поиска", "scale.md")]:
        path = ROOT / "eval" / "results" / name
        if path.exists():
            with st.expander(title):
                st.markdown(path.read_text(encoding="utf-8"))


def admin_page() -> None:
    inject_css()
    if not ctx()["is_admin"]:
        st.error("Раздел доступен только администратору.")
        return
    st.markdown("### Администрирование")
    tabs = st.tabs(["Журнал запросов", "Оценки", "Документы", "Качество", "Система"])
    with tabs[0]:
        journal()
    with tabs[1]:
        feedback()
    with tabs[2]:
        docs()
    with tabs[3]:
        quality()
    with tabs[4]:
        system()

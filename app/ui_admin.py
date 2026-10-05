"""Страница администрирования: журнал, оценки, документы, качество, состояние системы."""
import json
import re
import time

import pandas as pd
import streamlit as st

from app.ui_common import (ACCESS_LABELS, ROOT, ROUTE_LABELS, STATUS_LABELS, ctx, get_agent, health, index_meta,
                           inject_css)
from kbagent import auth
from kbagent.config import settings
from kbagent.actions import ACTIONS, load_actions
from kbagent.gaps import find_gaps
from kbagent.editions import compare
from kbagent.database import display
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
    df["time"] = df["time"].map(lambda t: display(t)[:16] if isinstance(t, str) else t)     # 05.10.2026 14:20
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
    st.dataframe(df, hide_index=True, use_container_width=True, height=min(480, 35 * (len(df) + 1) + 3),
                 column_config={"Ответ": st.column_config.TextColumn(width="large")})
    st.download_button("Выгрузить в CSV", _csv(df), file_name="zhurnal_zaprosov.csv", icon=":material/download:")


def _records() -> list[dict]:
    log = settings.log_dir / "audit.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[-20000:] if line.strip()]


def gaps() -> None:
    topics, total = find_gaps(_records())
    st.caption("Вопросы сотрудников, на которые в документах и учёте не нашлось ответа. Похожие формулировки "
               "собраны в одну тему — это список того, что стоит дописать в регламенты или загрузить в базу знаний.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Вопросов сотрудников", total["questions"])
    share = f" ({100 * total['unanswered'] / total['questions']:.0f} %)" if total["questions"] else ""
    c2.metric("Без ответа", f"{total['unanswered']}{share}")
    c3.metric("Тем без ответа", total["topics"])
    if not topics:
        st.info("Пока все вопросы сотрудников получили ответ.")
        return
    df = pd.DataFrame([{"Тема (как спрашивали чаще всего)": t["question"], "Раз": t["count"],
                        "Сотрудников": t["users"], "Роли": ", ".join(t["roles"]), "Последний раз": display(t["last"])[:16],
                        "Другие формулировки": "; ".join(t["other"]) or "—"} for t in topics])
    st.dataframe(df, hide_index=True, use_container_width=True,
                 column_config={"Тема (как спрашивали чаще всего)": st.column_config.TextColumn(width="large"),
                                "Другие формулировки": st.column_config.TextColumn(width="large")})
    st.download_button("Выгрузить в CSV", _csv(df), file_name="probely_v_dokumentacii.csv",
                       icon=":material/download:", key="gaps-csv")


def security() -> None:
    records = load_actions()
    st.caption("Кто, что и когда сделал: входы и неудачные попытки входа, загрузка, утверждение и удаление "
               "документов, выдача ключей API. Журнал только дописывается; хранится в logs/actions.jsonl на сервере.")
    st.caption(("Персональные данные в журнале вопросов и оценках (телефоны, почта, паспорт, СНИЛС, ФИО полностью) "
                "маскируются. " if settings.log_mask_pii else "Маскирование персональных данных выключено. ")
               + (f"Журнал вопросов хранится {settings.log_retention_days} дней."
                  if settings.log_retention_days else "Срок хранения журнала вопросов не ограничен."))
    day_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    recent = [r for r in records if r["time"] >= day_ago]
    c1, c2, c3 = st.columns(3)
    c1.metric("Входов за сутки", sum(r["action"] == "login" for r in recent))
    c2.metric("Неудачных попыток входа за сутки", sum(r["action"] == "login_failed" for r in recent))
    c3.metric("Изменений базы знаний за сутки", sum(r["action"] in ("upload", "approve", "remove") for r in recent))
    if not records:
        st.info("Действий пока нет.")
        return
    kinds = st.multiselect("События", list(ACTIONS.values()), placeholder="Все")
    df = pd.DataFrame(records[::-1]).reindex(columns=["time", "action", "user", "target", "details"])
    df["time"] = df["time"].map(lambda t: display(t)[:16])
    df["action"] = df["action"].map(lambda a: ACTIONS.get(a, a))
    if kinds:
        df = df[df["action"].isin(kinds)]
    df = df.fillna("—").replace("", "—")
    df.columns = ["Время", "Событие", "Пользователь", "Объект", "Подробности"]
    st.dataframe(df, hide_index=True, use_container_width=True, height=min(420, 35 * (len(df) + 1) + 3))
    st.download_button("Выгрузить в CSV", _csv(df), file_name="zhurnal_deystviy.csv", icon=":material/download:",
                       key="actions-csv")


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
                        cmp = compare(replaces, res["id"])
                        notices.append(("info", f"По сравнению с прежней редакцией: изменено пунктов — "
                                                f"{cmp.count('changed')}, добавлено — {cmp.count('added')}, "
                                                f"исключено — {cmp.count('removed')}. Подробно — в чате: "
                                                f"«Что изменилось в {res['title']}?»"))
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
                remove_upload(agent.index, d["id"], user=ctx()["login"])
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


def _report(path) -> dict | None:
    """Сводка отчёта eval/results/*.md: верно из всего, метрики, медиана времени, текст без заголовка."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    total = re.search(r"вопросов: (\d+)", text)
    return {"ok": len(re.findall(r"^\|[^|\n]+\|[^|\n]+\|[^|\n]+\| верно \|", text, re.M)),
            "total": int(total.group(1)) if total else 0,
            "metrics": dict(re.findall(r"^\| ([^|\n]+?) \| (\d+ из \d+|[\d.]+ с) \|$", text, re.M)),
            "body": text.split("\n", 1)[1] if text.startswith("# ") else text,
            "date": (re.search(r"\((\d{4}-\d{2}-\d{2})", text) or [None, ""])[1]}


def quality() -> None:
    results = ROOT / "eval" / "results"
    main, jury, roles = (_report(results / f"{n}.md") for n in ("latest", "jury", "roles"))
    laptop, small = _report(results / "laptop.md"), _report(results / "smallgpu.md")
    if main is None:
        st.info("Результатов проверки качества пока нет: python eval/run_eval.py")
        return
    m = main["metrics"]
    seconds = lambda r: (r["metrics"].get("Время ответа, медиана", "—").replace(".", ",") if r else "—")  # noqa: E731
    st.caption(f"Эталонный набор: {main['total']} вопросов с проверенными ответами. Ответ засчитывается, только если "
               "верные факты есть в самом тексте ответа и ссылка указывает на верный документ и пункт.")
    c = st.columns(4)
    c[0].metric("Верные ответы со ссылкой", m.get("Правильных ответов (факты и источник верны)", "—"))
    c[1].metric("Честные отказы, когда ответа нет", m.get("Верных отказов на вопросы без ответа", "—"))
    c[2].metric("Ответы по учётной базе", m.get("Ответов по учётной базе", "—"))
    c[3].metric("Неверные ответы", m.get("Неверных ответов, выданных как ответ", "—"))
    c = st.columns(4)
    c[0].metric("Вопросы «с ходу», как от жюри", f"{jury['ok']} из {jury['total']}" if jury else "—")
    c[1].metric("Разграничение доступа по ролям", f"{roles['ok']} из {roles['total']}" if roles else "—")
    c[2].metric("Ответ на видеокарте, медиана", seconds(main))
    c[3].metric("Ноутбук: без видеокарты / 4 ГБ", f"{seconds(laptop)} / {seconds(small)}")
    for title, rep in [(f"Эталон — все {main['total']} вопросов", main), ("Вопросы «с ходу», как от жюри", jury),
                       ("Разграничение доступа по ролям", roles), ("Эталон на ноутбуке без видеокарты", laptop),
                       ("Эталон на ноутбуке с небольшой видеокартой", small)]:
        if rep:
            with st.expander(f"{title} — {rep['ok']} из {rep['total']}, прогон {display(rep['date'])}"):
                st.markdown(rep["body"])


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
    tabs = st.tabs(["Журнал запросов", "Пробелы в документации", "Оценки", "Документы", "Качество",
                    "Безопасность", "Система"])
    with tabs[0]:
        journal()
    with tabs[1]:
        gaps()
    with tabs[2]:
        feedback()
    with tabs[3]:
        docs()
    with tabs[4]:
        quality()
    with tabs[5]:
        security()
    with tabs[6]:
        system()

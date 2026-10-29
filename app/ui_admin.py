"""Страница администрирования: журнал, оценки, документы, качество, состояние системы."""
import json
import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui_common import (ACCESS_LABELS, APP_NAME, ROOT, ROUTE_LABELS, STATUS_LABELS, ctx, get_agent, health,
                           index_meta, inject_css)
from kbagent import auth
from kbagent.config import settings
from kbagent.actions import ACTIONS, load_actions
from kbagent.gaps import find_gaps
from kbagent.editions import compare
from kbagent.database import display
from kbagent.ingest import ocr_available, registry_info
from kbagent.uploads import (UPLOAD_TYPES, add_upload, approve_upload, documents, list_uploads, remove_upload,
                             similar_documents)
from kbagent import (backup, demand, effect, experts, folder_sync, jobs, onec, pilot_report, prajs,
                     spravochnik, tkp_registry, vnedrenie)
from kbagent.actions import log_action

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
    st.dataframe(df, hide_index=True, width="stretch", height=min(480, 35 * (len(df) + 1) + 3),
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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Вопросов сотрудников", total["questions"])
    share = f" ({100 * total['unanswered'] / total['questions']:.0f} %)" if total["questions"] else ""
    c2.metric("Без ответа", f"{total['unanswered']}{share}")
    c3.metric("Открытых тем", total["topics"])
    c4.metric("Закрыто тем", total["closed"], help="Позже на этот вопрос нашёлся ответ — например, после загрузки регламента")
    opened = [t for t in topics if not t["closed"]]
    closed = [t for t in topics if t["closed"]]
    if opened:
        df = pd.DataFrame([{"Тема (как спрашивали чаще всего)": t["question"], "Раз": t["count"],
                            "Сотрудников": t["users"], "Роли": ", ".join(t["roles"]),
                            "Последний раз": display(t["last"])[:16],
                            "Другие формулировки": "; ".join(t["other"]) or "—",
                            "Ответ есть для ролей": ", ".join(t["other_roles"]) or "—"} for t in opened])
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Тема (как спрашивали чаще всего)": st.column_config.TextColumn(width="large"),
                                    "Другие формулировки": st.column_config.TextColumn(width="large"),
                                    "Ответ есть для ролей": st.column_config.TextColumn(
                                        help="Документ есть, но закрыт для роли, которая спрашивала: "
                                             "это вопрос прав доступа, а не пробел в документах")})
        st.download_button("Выгрузить в CSV", _csv(df), file_name="probely_v_dokumentacii.csv",
                           icon=":material/download:", key="gaps-csv")
    else:
        st.info("Открытых пробелов нет: на все вопросы сотрудников агент сейчас отвечает.")
    if closed:
        with st.expander(f"Закрытые пробелы: {len(closed)} — на эти вопросы агент теперь отвечает"):
            st.dataframe(pd.DataFrame([{"Тема": t["question"], "Раз спрашивали": t["count"],
                                        "Роли": ", ".join(t["roles"]), "Без ответа до": display(t["last"])[:16],
                                        "Закрыт": display(t["closed"])[:16]} for t in closed]),
                         hide_index=True, width="stretch",
                         column_config={"Тема": st.column_config.TextColumn(width="large")})


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
    st.dataframe(df, hide_index=True, width="stretch", height=min(420, 35 * (len(df) + 1) + 3))
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
    st.dataframe(df, hide_index=True, width="stretch")
    st.caption("Вопросы с 👎 — кандидаты в эталонный набор (eval/questions.yaml): после исправления "
               "они проверяются при каждом прогоне.")
    st.download_button("Выгрузить в CSV", _csv(df), file_name="otsenki.csv", icon=":material/download:")


def docs_folder(agent) -> None:
    """Папка документов отдела: новые и изменённые файлы подключаются сами, удалённые — убираются."""
    with st.container(border=True):
        st.markdown("**Папка документов отдела**")
        folder = settings.docs_folder
        if not folder:
            st.caption("Не задана. Подключите сетевую или локальную папку на вкладке «Внедрение» → «Подключения» — "
                       "новые и изменённые файлы будут подключаться сами (по расписанию, scripts/install_tasks.bat), "
                       "удалённые — убираться из базы знаний.")
            return
        last = folder_sync.last_report()
        st.caption(f"Папка: {folder}. Реестр — catalog.yaml в папке или загруженный шаблон партнёра; без записи "
                   "в реестре название берётся из документа, доступ — " + ACCESS_LABELS.get(
                       settings.docs_folder_access, settings.docs_folder_access).lower() + ".")
        if last:
            st.caption(f"Последняя проверка: {display(last['started'])[:16]} — добавлено {last['added']}, обновлено "
                       f"{last['updated']}, удалено {last['removed']}, без изменений {last['unchanged']}.")
            if last.get("warning"):
                st.warning(last["warning"])
            for name, error in last.get("errors", []):
                st.error(f"{name}: {error}")
        if st.button("Проверить папку сейчас", icon=":material/sync:", key="folder-sync"):
            with st.spinner("Сверяю папку с базой знаний…"):
                rep = folder_sync.sync(agent.index, Path(folder), user=ctx()["login"])
            st.session_state.folder_note = (
                f"Добавлено {len(rep.added)}, обновлено {len(rep.updated)}, удалено {len(rep.removed)}, без "
                f"изменений {rep.unchanged}" + (f", ошибок {len(rep.errors)}" if rep.errors else "") + ".")
            st.rerun()
        if note := st.session_state.pop("folder_note", None):
            st.success(note)


def docs() -> None:
    agent = get_agent()
    agent.index.reload_if_changed(settings.index_dir)      # документы могла добавить обработка папки
    docs_folder(agent)
    with st.container(border=True):
        st.markdown("**Добавить документ**")
        st.caption("PDF и Word; сканы и фото страниц — текст распознаётся" + ("" if ocr_available() else
                   " (распознавание сейчас не установлено)") + "; таблица Excel — каждая строка станет записью "
                   "со ссылкой на лист и строку. До утверждения документ помечается «загружен пользователем, "
                   "не утверждён».")
        all_docs = documents(agent.index)
        active = {d["id"]: d["title"] for d in all_docs if d["status"] == "действует"}
        c1, c2 = st.columns(2)
        access = c1.selectbox("Кому доступен", [k for k in ACCESS_LABELS if k != "archive"],
                              format_func=ACCESS_LABELS.get)
        r1, r2, r3 = st.columns(3)
        version = r1.text_input("Версия (редакция)", placeholder="например, 2026 или Изм. 2")
        start = r2.date_input("Действует с", value=None, format="DD.MM.YYYY",
                              help="До этой даты документ видит только администратор")
        owner = r3.text_input("Владелец документа", placeholder="кому направлять вопросы и противоречия")
        replaces = c2.selectbox("Новая редакция документа", [None] + list(active),
                                format_func=lambda x: "— не заменяет другой документ —" if x is None else active[x],
                                help="До утверждения новую редакцию видит только администратор, сотрудники получают ответы по действующей. После «Утвердить» прежняя редакция отменяется и перестаёт участвовать в поиске")
        st.session_state.setdefault("upload_key", 0)
        files = st.file_uploader("Файлы", type=UPLOAD_TYPES, accept_multiple_files=True,
                                 key=f"uploader-{st.session_state.upload_key}", label_visibility="collapsed")
        if files:
            notices = st.session_state.setdefault("notices", [])
            for f in files:
                bar = st.progress(0.0, text=f"Читаю «{f.name}»…")
                try:
                    res = add_upload(agent.index, f.name, f.getvalue(), access=access, user=ctx()["login"],
                                     replaces=replaces, version=version.strip(), owner=owner.strip(),
                                     effective_from=start.isoformat() if start else "",
                                     progress=lambda done, total, b=bar, name=f.name:
                                     b.progress(done / total, text=f"«{name}»: {done} из {total} фрагментов"))
                    notices.append(("success", f"«{res['title']}» добавлен: {res['chunks']} фрагментов")
                                   if res["chunks"] else ("warning", f"В «{res['title']}» не найден текст"))
                    if res.get("ocr_pages"):
                        notices.append(("info", f"Распознано страниц-сканов: {res['ocr_pages']}"))
                    if res.get("table_rows") is not None:
                        notices.append(("info", f"Таблица Excel: {res['table_rows']} строк — каждая строка "
                                                "стала записью со ссылкой на лист и строку"))
                    if res["replaced_same"]:
                        notices.append(("warning", f"Заменён ранее загруженный «{res['title']}»"))
                    if not replaces:
                        for other in similar_documents(res["title"], exclude={res["id"]}):
                            notices.append(("warning", f"Похоже на новую редакцию «{other}». Если это так, удалите "
                                                       "документ и загрузите заново с отметкой «Новая редакция "
                                                       "документа» — иначе в поиске будут обе редакции."))
                    if replaces:
                        notices.append(("info", f"Это проект новой редакции «{active[replaces]}»: пока его видит "
                                                f"только администратор, сотрудники получают ответы по действующей. "
                                                f"После «Утвердить» прежняя редакция будет отменена."))
                        cmp = compare(replaces, res["id"])
                        notices.append(("info", f"По сравнению с действующей редакцией: изменено пунктов — "
                                                f"{cmp.count('changed')}, добавлено — {cmp.count('added')}, "
                                                f"исключено — {cmp.count('removed')}. Подробно — в чате после "
                                                f"утверждения: «Что изменилось в {res['title']}?»"))
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
            state = ("утверждён" if d.get("approved") else
                     "проект новой редакции — виден только администратору" if d.get("replaces") else "ждёт утверждения")
            c1.markdown(f"{d['title']}  \n:gray[{ACCESS_LABELS.get(d.get('access'), d.get('access'))} · "
                        f"загрузил {d.get('uploaded_by') or '—'} {d.get('uploaded_at', '')} · {state}]")
            if not d.get("approved") and c2.button("Утвердить", key=f"ok-{d['id']}", icon=":material/check:"):
                approve_upload(agent.index, d["id"], user=ctx()["login"])
                st.rerun()
            if c3.button(":material/delete:", key=f"rm-{d['id']}", help="Удалить из базы знаний"):
                remove_upload(agent.index, d["id"], user=ctx()["login"])
                st.rerun()

    df = pd.DataFrame(documents(agent.index)).reindex(columns=["title", "version", "effective", "owner", "priority",
                                                                "access", "status", "replaced_by"])
    titles = {d["id"]: d["title"] for d in documents(agent.index)}
    df["access"] = df["access"].map(lambda a: ACCESS_LABELS.get(a, a))
    df["replaced_by"] = df["replaced_by"].map(lambda r: titles.get(r, r) if isinstance(r, str) else "—")
    df = df.fillna("—")
    df.columns = ["Документ", "Версия", "Срок действия", "Владелец", "Приоритет", "Доступ", "Статус", "Заменён на"]
    st.markdown("**Реестр документов базы знаний**")
    st.dataframe(df, hide_index=True, width="stretch")
    st.caption("Отменённые редакции хранятся для истории, но в поиске не участвуют. Документ, который ещё не вступил "
               "в силу, видит только администратор. Если срок действия истёк, ссылка и ответ помечаются "
               "предупреждением — замените документ новой редакцией. Приоритет 1 — главный источник при "
               "расхождении (например, регламент или справочник параметров главнее руководства).")


def reference() -> None:
    """Справочник параметров: когда обновлён, что не прошло проверку, где источники расходятся."""
    values, rep = spravochnik.load()
    st.caption("Параметры продукции для ответов техническим специалистам: точное значение, единица и источник — "
               "документ и страница. Агент только читает справочник; сравнение с требованием клиента и подбор "
               "выполняет программа. Файл — CSV или XLSX с колонками: " + ", ".join(spravochnik.COLUMNS) + ".")
    c = st.columns(4)
    c[0].metric("Значений", rep.values)
    c[1].metric("Моделей", rep.models)
    c[2].metric("Строк с ошибками", len(rep.errors))
    c[3].metric("Противоречий", len(rep.contradictions))
    updated = display(rep.loaded_at)[:16] if rep.loaded_at else "—"
    st.caption(f"Файл: {rep.source or '—'} · проверен {updated} · повторов пропущено: {rep.duplicates}")
    if rep.errors:
        st.warning("Строки, не прошедшие проверку, не загружены:")
        st.dataframe(pd.DataFrame(rep.errors).rename(columns={"line": "Строка файла", "error": "Причина"}),
                     hide_index=True, width="stretch")
    if rep.contradictions:
        st.info("Источники расходятся — агент показывает оба значения и передаёт вопрос эксперту:")
        st.dataframe(pd.DataFrame([{"Модель": x["model"], "Параметр": x["param"],
                                    "Значения": "; ".join(f"{v['value']} (с. {v['page']})" for v in x["values"])}
                                   for x in rep.contradictions]), hide_index=True, width="stretch")
    with st.container(border=True):
        st.markdown("**Загрузить новую версию справочника**")
        st.caption("Файл проверяется до замены: пропуски, единицы, числа, страницы, документы. Повторяющиеся строки "
                   "не дублируются. Если ни одна строка не прошла проверку, действующий справочник остаётся.")
        st.session_state.setdefault("spr_key", 0)
        f = st.file_uploader("Справочник", type=["csv", "xlsx"], key=f"spr-{st.session_state.spr_key}",
                             label_visibility="collapsed")
        if f is not None:
            try:
                new = spravochnik.replace_file(f.name, f.getvalue())
                log_action("spravochnik", ctx()["login"], f.name,
                           f"значений: {new.values}; ошибок: {len(new.errors)}; противоречий: {len(new.contradictions)}")
                st.session_state.spr_note = ("success", f"Загружено значений: {new.values}, моделей: {new.models}. "
                                                        f"Ошибок: {len(new.errors)}, противоречий: {len(new.contradictions)}.")
            except ValueError as e:
                st.session_state.spr_note = ("error", str(e))
            st.session_state.spr_key += 1
            st.rerun()
        if note := st.session_state.pop("spr_note", None):
            getattr(st, note[0])(note[1])
    if values:
        df = pd.DataFrame([{"Модель": v.model, "Вид": v.kind, "Параметр": v.param, "Значение": v.shown(),
                            "Документ": v.doc, "Страница": v.page} for v in values])
        flt = st.text_input("Найти модель или параметр", key="spr-filter", placeholder="например, 4143 или полоса")
        if flt:
            df = df[df.apply(lambda r: flt.lower() in f"{r['Модель']} {r['Параметр']}".lower(), axis=1)]
        st.dataframe(df, hide_index=True, width="stretch", height=360)
        st.download_button("Скачать справочник (CSV)", spravochnik.PATH.read_bytes(), file_name="spravochnik.csv",
                           icon=":material/download:", key="spr-dl")
    price_list()


def price_list() -> None:
    """Прайс для ТКП: цена и срок поставки по моделям — с проверкой при загрузке, как у справочника."""
    st.markdown("#### Прайс для ТКП")
    prices, rep = prajs.load()
    values, _ = spravochnik.load()
    without = sorted({v.model for v in values} - set(prices))
    st.caption("Цена за единицу и срок поставки подставляются в разбор письма клиента и в ТКП с пометкой «по прайсу "
               "от …»; сумму считает программа, утверждает менеджер. Файл — CSV или XLSX с колонками: "
               + ", ".join(prajs.COLUMNS) + f". Прайс старше {prajs.STALE_DAYS} дней отмечается в ТКП.")
    c = st.columns(4)
    c[0].metric("Цен", len(prices))
    c[1].metric("Моделей без цены", len(without), help=", ".join(without) or None)
    c[2].metric("Строк с ошибками", len(rep.errors) if prices else 0)
    c[3].metric("Разные цены одной модели", len(rep.conflicts))
    if not prices:
        st.info("Прайс не загружен — цены и сроки в ТКП вписывает менеджер.")
    if rep.errors and prices:
        st.warning("Строки, не прошедшие проверку, не загружены:")
        st.dataframe(pd.DataFrame(rep.errors).rename(columns={"line": "Строка файла", "error": "Причина"}),
                     hide_index=True, width="stretch")
    if rep.conflicts:
        st.warning("У этих моделей в прайсе разные цены — цена в ТКП не подставляется, пока прайс не исправят: "
                   + "; ".join(f"{x['model']}: {', '.join(x['prices'])}" for x in rep.conflicts))
    if rep.unknown:
        st.info("Этих моделей нет в справочнике параметров — в ТКП они не попадут: " + ", ".join(rep.unknown))
    price_from_1c()
    with st.container(border=True):
        st.markdown("**Загрузить новый прайс**")
        st.caption("Файл проверяется до замены: пропуски, цены, даты, разные цены одной модели. Без колонки "
                   "«Действует с» датой прайса считается день загрузки. Если ни одна строка не прошла проверку, "
                   "действующий прайс остаётся.")
        st.session_state.setdefault("prajs_key", 0)
        f = st.file_uploader("Прайс", type=["csv", "xlsx"], key=f"prajs-{st.session_state.prajs_key}",
                             label_visibility="collapsed")
        if f is not None:
            try:
                new = prajs.replace_file(f.name, f.getvalue())
                log_action("prajs", ctx()["login"], f.name,
                           f"цен: {new.prices}; ошибок: {len(new.errors)}; разных цен: {len(new.conflicts)}")
                conflicts = (f" Разные цены — модели не загружены: {', '.join(x['model'] for x in new.conflicts)}."
                             if new.conflicts else "")
                st.session_state.prajs_note = ("success", f"Загружено цен: {new.prices}. Ошибок: {len(new.errors)}."
                                                          + conflicts)
            except ValueError as e:
                st.session_state.prajs_note = ("error", str(e))
            st.session_state.prajs_key += 1
            st.rerun()
        if note := st.session_state.pop("prajs_note", None):
            getattr(st, note[0])(note[1])
    if prices:
        df = pd.DataFrame([{"Модель": p.model, "Цена": prajs.money(p.price), "Срок поставки": p.lead,
                            "Действует с": prajs.shown_date(p.since) + (" — проверить" if p.stale else ""),
                            "Примечание": p.note} for p in prices.values()])
        st.dataframe(df, hide_index=True, width="stretch", height=300)
        st.download_button("Скачать прайс (CSV)", prajs.PATH.read_bytes(), file_name="prajs.csv",
                           icon=":material/download:", key="prajs-dl")


def price_from_1c() -> None:
    """Цены и остатки из 1С — только чтение; отчёт последнего обновления."""
    with st.container(border=True):
        st.markdown("**Цены из 1С**")
        c = onec.conf()
        if not onec.configured():
            st.caption("Не подключено. Цены (и остатки) можно брать из 1С вместо файла — только чтение, через "
                       "стандартный интерфейс OData: KB_1C_URL (…/odata/standard.odata), KB_1C_USER, пароль — "
                       "KB_1C_PASSWORD или data/1c_password.txt, KB_1C_PRICE_TYPE — тип цен, KB_1C_CONFIG — bp "
                       "(Бухгалтерия 3.0) или ut (УТ 11, КА 2, ERP), KB_1C_STOCK=1 — остатки. Что опубликовать в "
                       "1С — `docs/vnedrenie.md`.")
            return
        st.caption(f"{c['title']} · тип цен «{c['price_type']}»" + (" · остатки" if c["stock_on"] else "")
                   + ". Номенклатура сопоставляется с моделями справочника по артикулу или наименованию; срок "
                   "поставки — «в наличии», если есть остаток, иначе — как в прайсе. В 1С ничего не меняется.")
        if st.button("Обновить цены из 1С", icon=":material/sync:", key="prajs-1c"):
            try:
                with st.spinner("Читаю 1С…"):
                    rep = onec.sync()
                log_action("prajs_1c", ctx()["login"], rep["price_type"],
                           f"цен: {rep['prices']}; без цены: {len(rep['without_price'])}")
                st.session_state.onec_note = ("success", f"Из 1С загружено цен: {rep['prices']}.")
            except onec.OneCError as e:
                st.session_state.onec_note = ("error", str(e))
            st.rerun()
        if note := st.session_state.pop("onec_note", None):
            getattr(st, note[0])(note[1])
        rep = onec.last_report()
        if not rep:
            return
        st.caption(f"Последнее обновление: {display(rep['time'])[:16]} · сервер {rep['server']} · номенклатуры "
                   f"в 1С: {rep['items']} · сопоставлено с моделями: {rep['matched']}")
        for w in rep["warnings"]:
            st.warning(w, icon=":material/warning:")
        if rep["ambiguous"]:
            st.info("Позиции 1С подходят сразу к нескольким моделям — не загружены (уточните наименование или "
                    "артикул): " + "; ".join(rep["ambiguous"][:8]))
        if rep["foreign"]:
            st.info("Цена в валюте — не подставляется: " + ", ".join(rep["foreign"][:8]))


def expert_queue() -> None:
    """Вопросы эксперту: противоречия между источниками и вопросы, переданные сотрудниками вручную."""
    items = experts.load()
    open_items = [x for x in items if x["status"] == "открыт"]
    st.caption("Когда источники расходятся, агент показывает оба значения, не делает вывода и передаёт вопрос "
               "эксперту — владельцу документа или справочника. Сотрудник может передать эксперту и любой ответ, "
               "в котором сомневается (кнопка «Эксперту» под ответом). Решение эксперта появляется под ответом "
               "в чате и фиксируется в журнале действий.")
    c = st.columns(2)
    c[0].metric("Открытых вопросов", len(open_items))
    c[1].metric("Решено", len(items) - len(open_items))
    if not items:
        st.info("Вопросов эксперту пока нет.")
        return
    for it in sorted(items, key=lambda x: (x["status"] != "открыт", x["time"]), reverse=False):
        with st.container(border=True):
            details = it["details"] if isinstance(it["details"], str) else "; ".join(
                f"{d.get('value')} — {registry_info(d.get('doc', '')).get('title', d.get('doc'))}, с. {d.get('page')}"
                for d in it["details"])
            st.markdown(f"**{it['subject']}** · {it['kind']}  \n:gray[{display(it['time'])[:16]} · обращений: "
                        f"{it.get('asked', 1)} · {it['status']}]")
            st.caption(details if isinstance(it["details"], str) else f"Значения: {details}")
            if it.get("question") and it["question"] != it["subject"]:
                st.caption(f"Вопрос сотрудника: «{it['question']}»")
            if it.get("comment"):
                st.caption(f"Что вызывает сомнение: {it['comment']}")
            if it["status"] == "открыт":
                chosen = None
                if isinstance(it["details"], list) and len(it["details"]) > 1:
                    options = list(range(len(it["details"]))) + [None]
                    pick = st.radio(
                        "Какое значение верное", options, key=f"exp-pick-{it['id']}", horizontal=True,
                        index=len(options) - 1,          # выбор значения — только явным нажатием эксперта
                        format_func=lambda k, d=it["details"]: "оба верны / решение текстом" if k is None else
                        f"{d[k]['value']} (с. {d[k]['page']})",
                        help="Выбранным значением агент дальше отвечает сам, с пометкой «подтверждено экспертом»; "
                             "ответы клиентам, утверждённые с другим значением, попадут в «Эффект» на перепроверку")
                    chosen = it["details"][pick] if pick is not None else None
                note = st.text_input("Решение эксперта", key=f"exp-note-{it['id']}",
                                     placeholder="например: верно 1 Гвыб/с, в таблице на с. 6 опечатка")
                if st.button("Решено", key=f"exp-ok-{it['id']}", icon=":material/check:"):
                    experts.resolve(it["id"], ctx()["name"], note, chosen)
                    log_action("expert", ctx()["login"], it["subject"],
                               (f"верно: {chosen['value']}; " if chosen else "") + note)
                    st.rerun()
            else:
                if it.get("chosen"):
                    st.caption(f"Верное значение: {it['chosen']['value']} (с. {it['chosen']['page']}) — дальше агент "
                               "отвечает им сам, с пометкой «подтверждено экспертом»")
                st.caption(f"Решил {it.get('resolved_by') or '—'} {display(it.get('resolved_at', ''))[:16]}: "
                           f"{it.get('note') or '—'}")


def pilot_report_box() -> None:
    """Отчёт о пилоте в Word за период — для заказчика, отзыва и заявки на грант."""
    import datetime as dt
    with st.container(border=True):
        c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
        today = dt.date.today()
        period = c1.date_input("Отчёт о пилоте за период", (today - dt.timedelta(days=30), today),
                               max_value=today, format="DD.MM.YYYY", key="pilot-period")
        if not (isinstance(period, tuple) and len(period) == 2):
            c2.caption("Выберите начало и конец периода")
            return
        if c2.button("Сформировать отчёт (Word)", icon=":material/summarize:", key="pilot-make", width="stretch"):
            data = pilot_report.collect(*period)
            st.session_state.pilot_docx = (pilot_report.report_docx(
                data, tkp_registry.load_settings().get("company", ""),
                "AI-агент для поиска ответов по внутренней документации и базам данных предприятия"), period)
            log_action("pilot_report", ctx()["login"], f"{period[0]:%d.%m.%Y} — {period[1]:%d.%m.%Y}",
                       f"вопросов: {data['questions']}")
        st.caption("Вопросы, доля ответов со ссылкой на источник, оценки, время до утверждения против ручной "
                   "подготовки, пробелы, эксперт, ТКП и спрос, качество на проверочных наборах, безопасность; в "
                   "конце — место для заключения и подписей заказчика. Без персональных данных.")
        if made := st.session_state.get("pilot_docx"):
            doc, (since, until) = made
            st.download_button("Скачать отчёт о пилоте", doc, file_name=f"otchet_pilot_{since:%Y%m%d}_{until:%Y%m%d}.docx",
                               type="primary", icon=":material/download:", key="pilot-dl",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def effect_tab() -> None:
    """Польза в цифрах: время от вопроса до утверждённого ответа или ТКП против ручной подготовки."""
    summary = effect.summary()
    records = effect.load()
    st.caption("Мерило партнёра — время подготовки проверенного ответа (цель — минус 50 %). Агент сам замеряет "
               "время от вопроса до утверждения сотрудником. Ручное время — оценка до замера: 15 мин на ответ, "
               "60 мин на ТКП (`docs/pilot/zamer_i_stoimost.md`); после замера на запросах партнёра заменяется "
               "измеренным.")
    kinds = summary["kinds"]
    c = st.columns(4)
    total = sum(k["count"] for k in kinds.values())
    c[0].metric("Утверждено ответов и ТКП", total)
    answers, offers = kinds.get("ответ"), kinds.get("ТКП")
    fmt = lambda m: f"{m * 60:.0f} с" if m < 1 else f"{m:.1f}".replace(".", ",") + " мин"  # noqa: E731
    for col, title, k in ((c[1], "Ответ клиенту, медиана", answers), (c[2], "ТКП, медиана", offers)):
        col.metric(title, fmt(k["median_min"]) if k else "—",
                   delta=f"вручную — {k['baseline_min']} мин (оценка)" if k else None, delta_color="off",
                   delta_arrow="off",
                   help=f"Цель партнёра — не более {k['baseline_min'] / 2:g} мин (минус 50 %)".replace(".", ",")
                   if k else None)
    c[3].metric("Сэкономлено (оценка)", f"{summary['saved_hours']:.1f} ч".replace(".", ","),
                help=f"≈ {summary['saved_rub']:,} ₽ при {effect.HOUR_COST} ₽ за час инженера".replace(",", " "))
    items = experts.load()
    done = [x for x in items if x["status"] == "решён"]
    c = st.columns(4)
    c[0].metric("Вопросов эксперту", len(items))
    c[1].metric("Решено экспертом", len(done))
    c[2].metric("Значений подтверждено экспертом", sum(1 for x in done if x.get("chosen")),
                help="Дальше агент отвечает этими значениями сам — эксперту тот же вопрос не задают")
    stale = effect.affected(records)
    c[3].metric("Ответов на перепроверку", len(stale),
                help="Источник изменился после утверждения: новая редакция, истёк срок, эксперт исправил значение")
    if stale:
        st.warning("Ответы клиентам, которые нужно перепроверить — их источники изменились:",
                   icon=":material/sync_problem:")
        st.dataframe(pd.DataFrame([{"Когда": display(r["time"])[:16], "Что": r["kind"], "Сотрудник": r["user"],
                                    "Вопрос": r["question"][:90], "Почему": "; ".join(r["reasons"])}
                                   for r in reversed(stale)]), hide_index=True, width="stretch")
    if records:
        st.markdown("**Утверждённые ответы и ТКП**")
        st.dataframe(pd.DataFrame([{"Когда": display(r["time"])[:16], "Что": r["kind"], "Сотрудник": r["user"],
                                    "Вопрос": r["question"][:90],
                                    "От вопроса до утверждения": fmt(r["seconds"] / 60),
                                    "Позиций": r.get("positions") or "—"} for r in reversed(records)]),
                     hide_index=True, width="stretch")
    else:
        st.info("Пока ничего не утверждено: кнопка «Черновик» или «Черновик ТКП» под ответом в чате → "
                "«Утвердить и скачать».")
    pilot_report_box()


def _report(path) -> dict | None:
    """Сводка отчёта eval/results/*.md: верно из всего, метрики, медиана времени, текст без заголовка."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    total = re.search(r"вопросов: (\d+)", text)
    ok = len(re.findall(r"^\|[^|\n]+\|[^|\n]+\|[^|\n]+\| верно \|", text, re.M))
    total = int(total.group(1)) if total else 0
    if counts := pilot_report.eval_counts(path):         # по CSV: «|» в письме клиента ломает строку Markdown
        ok, total = counts
    return {"ok": ok, "total": total,
            "metrics": dict(re.findall(r"^\| ([^|\n]+?) \| (\d+ из \d+|[\d.]+ с) \|$", text, re.M)),
            "body": text.split("\n", 1)[1] if text.startswith("# ") else text,
            "date": (re.search(r"\((\d{4}-\d{2}-\d{2})", text) or [None, ""])[1]}


def quality() -> None:
    results = ROOT / "eval" / "results"
    main, jury, roles = (_report(results / f"{n}.md") for n in ("latest", "jury", "roles"))
    laptop, small = _report(results / "laptop.md"), _report(results / "smallgpu.md")
    notebook = _report(results / "laptop3050.md")        # настоящий ноутбук RTX 3050 4 ГБ (участник 2)
    spr, offers = _report(results / "spravochnik.md"), _report(results / "tkp.md")
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
    c[3].metric("Ноутбук с видеокартой 4 ГБ, медиана", seconds(notebook) if notebook else seconds(small),
                help="Ноутбук RTX 3050 4 ГБ (режим -SmallGpu). Без видеокарты NVIDIA ответ — около минуты")
    c = st.columns(4)
    c[0].metric("Справочник параметров", f"{spr['ok']} из {spr['total']}" if spr else "—",
                help="Значение, проверка требования, подбор, сравнение, противоречие источников — eval/spravochnik.yaml")
    c[1].metric("Письма клиентов → ТКП", f"{offers['ok']} из {offers['total']}" if offers else "—",
                help="Позиции, количество, предложенная модель и источники в разборе письма — eval/tkp.yaml")
    for title, rep in [(f"Эталон — все {main['total']} вопросов", main), ("Вопросы «с ходу», как от жюри", jury),
                       ("Разграничение доступа по ролям", roles), ("Справочник параметров", spr),
                       ("Письма клиентов → ТКП", offers),
                       ("Эталон на ноутбуке RTX 3050 4 ГБ", notebook),
                       ("Эталон на ПК в режиме видеокарты 4 ГБ (-SmallGpu)", small),
                       ("Эталон на ПК, модель в оперативной памяти (-Cpu)", laptop)]:
        if rep:
            with st.expander(f"{title} — {rep['ok']} из {rep['total']}, прогон {display(rep['date'])}"):
                st.markdown(rep["body"])


JOB_STATES = {"ok": ":green[● работает]", "late": ":orange[● отстала]", "error": ":red[● ошибка]",
              "never": ":gray[○ не запускалась]", "off": ":gray[— не настроена]", "running": ":blue[● идёт сейчас]"}


@st.cache_data(ttl=60, show_spinner=False)
def scheduled_tasks() -> dict[str, bool] | None:
    return jobs.scheduled()


def uptime_panel() -> None:
    """Сторож: сколько каждая служба работает без сбоев, перезапуски за неделю, последняя ошибка."""
    from kbagent import watchdog
    rows = watchdog.summary()
    if not rows:
        st.caption("Автозапуск и перезапуск при сбоях не включён: scripts\\install_tasks.bat регистрирует сторожа — при "
                   "входе в Windows и каждые 2 минуты он запускает модели и чат и перезапускает упавшее.")
        return
    with st.container(border=True):
        st.markdown("**Работа без сбоев**")
        for r in rows:
            state = (f":green[● работает] {jobs.ago(r['uptime']).replace(' назад', '')} без сбоев"
                     if r["up"] else ":red[● не отвечает]")
            if r["up"] and r["uptime"] is not None and r["uptime"] < 2:
                state = ":green[● работает] — только что запущен"
            extra = f"; перезапусков за неделю: {r['restarts_week']}" if r["restarts_week"] else ""
            st.markdown(f"{state} — **{r['title']}**{extra}")
            if not r["up"] and r["error"]:
                st.caption(f"Последняя ошибка: {r['error']}")
        if any(r["failing"] >= 2 for r in rows):
            st.warning("Служба не поднимается после перезапуска — смотрите журналы в папке logs (llm.log, chat.log).",
                       icon=":material/error:")


def jobs_panel() -> None:
    """Фоновые задачи: когда последний раз отработали, не отстали ли, есть ли в Планировщике; запуск вручную."""
    with st.container(border=True):
        st.markdown("**Фоновые задачи**")
        st.caption("Резервная копия раз в день, почта отдела, папка документов и цены из 1С — по расписанию "
                   "Windows: зарегистрировать — scripts\\install_tasks.bat (почта, папка и 1С — если настроены в "
                   "data\\settings.env). Итог каждого запуска — здесь.")
        rows = jobs.status(registered=scheduled_tasks())
        for r in rows:
            sched = {True: "В Планировщике.", False: ":orange[Нет в Планировщике.]", None: ""}[r["registered"]]
            when = f"успешно — {jobs.ago(r['age'])}" if r["last_ok"] else "успешных запусков не было"
            st.markdown(f"{JOB_STATES[r['state']]} — **{r['title']}**, {jobs.every(r['every'])}"
                        + (f"; {when}. {sched}" if r["state"] != "off" else ""))
        for w in jobs.warnings(rows):
            st.warning(w, icon=":material/schedule:")
        missing = [r["title"] for r in rows if r["registered"] is False and r["state"] != "off"]
        if missing:
            st.info("Не зарегистрированы в Планировщике: " + ", ".join(missing) + " — выполните "
                    "scripts\\install_tasks.bat на этом компьютере.")
        ready = [r["key"] for r in rows if r["state"] != "off"]
        c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
        key = c1.selectbox("Запустить сейчас", ready, key="job-pick", format_func=lambda k: jobs.JOBS[k].title)
        if c2.button("Запустить", icon=":material/play_arrow:", key="job-run", disabled=not ready):
            with st.spinner(f"{jobs.JOBS[key].title}…"):
                rec = jobs.run(key)
            log_action("job_run", ctx()["login"], jobs.JOBS[key].title, rec["message"][:200])
            st.session_state.job_note = (rec["ok"],
                                         f"{jobs.JOBS[key].title}: {rec['message'] or 'код ' + str(rec['code'])}")
            st.rerun()
        if note := st.session_state.pop("job_note", None):
            (st.success if note[0] else st.error)(note[1])
        with st.expander("Вывод последних запусков"):
            for r in rows:
                if r["output"]:
                    st.markdown(f"**{r['title']}** — {r['finished'] or '…'}")
                    st.code(r["output"], language=None)


STEP_MARKS = {True: ":green[:material/check_circle:]", False: ":orange[:material/radio_button_unchecked:]",
              None: ":gray[:material/remove:]"}


def rollout_tab() -> None:
    """Внедрение: что уже сделано у заказчика (видно по состоянию системы) и подключения папки, почты, 1С."""
    st.caption("Шаги проверяются сами по состоянию системы — отмечать вручную ничего не нужно. Папка отдела, почта и "
               "1С подключаются здесь же: настройки пишутся в data\\settings.env, их видят чат, REST API и задачи "
               "по расписанию.")
    items = vnedrenie.steps(models_ok=all(health().values()), registered=scheduled_tasks())
    done, total = vnedrenie.progress(items)
    st.progress(done / total if total else 0.0, text=f"Готово {done} из {total}"
                + (" — система готова к работе" if done == total else ""))
    for s in items:
        st.markdown(f"{STEP_MARKS[s.done]} **{s.title}**{' (если нужно)' if s.optional else ''} — {s.detail}"
                    + ("" if s.done else f"  \n:gray[{s.how}]"))
    connections()


def _saved(kind: str, values: dict, password: str, check) -> None:
    """Сохранить подключение, проверить его и показать итог после обновления страницы."""
    vnedrenie.save(values)
    if password:
        vnedrenie.save_password(kind, password)
    ok, message = check()
    log_action("settings", ctx()["login"], kind, ", ".join(k for k, v in values.items() if v)
               + ("; пароль обновлён" if password else "") + f"; проверка: {'да' if ok else 'нет'}")
    st.session_state.conn_note = ("success" if ok else "warning", f"Сохранено. Проверка: {message}.")
    st.rerun()


def connections() -> None:
    st.markdown("#### Подключения")
    if note := st.session_state.pop("conn_note", None):
        getattr(st, note[0])(note[1])
    with st.expander("Папка документов отдела", icon=":material/folder_shared:", expanded=not settings.docs_folder):
        with st.form("conn-folder"):
            path = st.text_input("Папка — сетевая или локальная", value=settings.docs_folder,
                                 placeholder=r"\\server\Документы\Техотдел")
            access = st.selectbox("Кому видны документы папки, пока их нет в реестре", list(ACCESS_LABELS)[:4],
                                  index=list(ACCESS_LABELS).index(settings.docs_folder_access)
                                  if settings.docs_folder_access in list(ACCESS_LABELS)[:4] else 0,
                                  format_func=ACCESS_LABELS.get)
            if st.form_submit_button("Сохранить и проверить", type="primary"):
                path = path.strip()
                _saved("folder", {"KB_DOCS_FOLDER": path, "KB_DOCS_FOLDER_ACCESS": access}, "",
                       lambda: vnedrenie.check_folder(path))
    with st.expander("Почтовый ящик отдела", icon=":material/mail:", expanded=False):
        st.caption("Только чтение: письма не помечаются прочитанными, ничего не удаляется и не отправляется. Для ИБ — "
                   "отдельная учётная запись ящика с правом только на чтение.")
        with st.form("conn-mail"):
            c1, c2 = st.columns([3, 1])
            host = c1.text_input("Сервер IMAP", value=settings.mail_host, placeholder="mail.zavod.ru")
            port = c2.text_input("Порт", value=str(settings.mail_port))
            c1, c2 = st.columns(2)
            user = c1.text_input("Ящик (логин)", value=settings.mail_user, placeholder="sales@zavod.ru")
            folder = c2.text_input("Папка", value=settings.mail_folder)
            password = st.text_input("Пароль" + (" — сохранён; введите, чтобы заменить"
                                                 if vnedrenie.has_password("mail") else ""), type="password")
            if st.form_submit_button("Сохранить и проверить", type="primary"):
                if not port.strip().isdigit():
                    st.error("Порт — число, обычно 993.")
                else:
                    _saved("mail", {"KB_MAIL_HOST": host, "KB_MAIL_PORT": port, "KB_MAIL_USER": user,
                                    "KB_MAIL_FOLDER": folder or "INBOX"}, password, vnedrenie.check_mail)
    with st.expander("Цены и остатки из 1С", icon=":material/sync:", expanded=False):
        st.caption("Стандартный интерфейс OData, только чтение — в 1С ничего не меняется. Что включить в 1С — "
                   "docs/vnedrenie.md, «Цены и остатки из 1С». Для ИБ — отдельный пользователь с правом только на "
                   "чтение.")
        with st.form("conn-1c"):
            url = st.text_input("Адрес OData", value=vnedrenie.current("KB_1C_URL"),
                                placeholder="http://1c-server/base/odata/standard.odata")
            c1, c2, c3 = st.columns(3)
            user = c1.text_input("Пользователь 1С", value=vnedrenie.current("KB_1C_USER"))
            ptype = c2.text_input("Тип цен", value=vnedrenie.current("KB_1C_PRICE_TYPE"), placeholder="Оптовая")
            presets = list(onec.PRESETS)
            config = c3.selectbox("Конфигурация", presets, format_func=lambda k: onec.PRESETS[k]["title"],
                                  index=presets.index(vnedrenie.current("KB_1C_CONFIG").lower())
                                  if vnedrenie.current("KB_1C_CONFIG").lower() in presets else 0)
            stock = st.toggle("Загружать остатки", value=vnedrenie.current("KB_1C_STOCK") in ("1", "true", "да"))
            password = st.text_input("Пароль" + (" — сохранён; введите, чтобы заменить"
                                                 if vnedrenie.has_password("1c") else ""), type="password")
            if st.form_submit_button("Сохранить и проверить", type="primary"):
                _saved("1c", {"KB_1C_URL": url, "KB_1C_USER": user, "KB_1C_PRICE_TYPE": ptype, "KB_1C_CONFIG": config,
                              "KB_1C_STOCK": "1" if stock else "0"}, password, vnedrenie.check_1c)


def system() -> None:
    names = {"llm": "Языковая модель GigaChat 3.1 Lightning", "embed": "Поиск по смыслу (bge-m3)",
             "rerank": "Оценка релевантности (bge-reranker-v2-m3)"}
    state = health()
    for key, name in names.items():
        st.markdown(f"{':green[● работает]' if state[key] else ':red[● не отвечает]'} — {name}")
    from kbagent import voice
    st.markdown((":green[● работает]" if voice.available() else ":gray[○ не установлено]")
                + " — Распознавание речи для голосовых вопросов (GigaAM v3, на процессоре)"
                + ("" if voice.available() else ": python scripts\\setup.py --voice, около 215 МБ"))
    if not all(state.values()):
        st.warning("Запустите серверы моделей: scripts\\start_servers.bat (на компьютере с видеокартой) "
                   "или scripts\\start_servers.bat -Cpu.")
    meta = index_meta()
    if meta:
        st.markdown(f"**База знаний:** {meta['documents']} документов, {meta['chunks']} фрагментов, "
                    f"обновлена {meta['built']}.")
    st.markdown("Все модели работают на этом компьютере, доступ в интернет не нужен.")
    uptime_panel()
    jobs_panel()
    with st.container(border=True):
        st.markdown("**Резервная копия данных**")
        st.caption("Учётные записи и ключи, журналы, диалоги, решения эксперта, загруженные документы и папка "
                   "отдела, справочник, прайс и индекс — архив с контрольными суммами в папке backups (хранятся 10 "
                   "последних). В архиве хэши паролей и журналы вопросов — храните его по правилам службы "
                   "безопасности. Восстановление и перенос в новую версию — scripts\backup.py restore.")
        if st.button("Создать резервную копию", icon=":material/backup:", key="backup-create"):
            with st.spinner("Собираю архив…"):
                path = backup.create()
            log_action("backup", ctx()["login"], path.name, backup.summary(backup.verify(path)))
            st.session_state.backup_path = str(path)
        if st.session_state.get("backup_path") and Path(st.session_state.backup_path).exists():
            path = Path(st.session_state.backup_path)
            st.success(f"Готово: {path.name}")
            st.download_button("Скачать архив", path.read_bytes(), file_name=path.name, mime="application/zip",
                               icon=":material/download:", key="backup-dl")
        archives = sorted((ROOT / "backups").glob("agent_backup_*.zip"), reverse=True)[:5]
        if archives:
            st.caption("Последние: " + "; ".join(f"{p.name} ({p.stat().st_size / 2**20:.1f} МБ)" for p in archives))
    for title, name in [("Скорость", "speed_DESKTOP-QMSBN1F.md"), ("Одновременные пользователи", "load.md"),
                        ("Масштаб поиска", "scale.md")]:
        path = ROOT / "eval" / "results" / name
        if path.exists():
            with st.expander(title):
                st.markdown(path.read_text(encoding="utf-8"))


PERIODS = {"30 дней": 30, "90 дней": 90, "Всё время": None}


def demand_tab() -> None:
    """Спрос: что запрашивают клиенты, какие запросы не закрыты и почему, причины проигрышей ТКП."""
    import datetime as dt
    st.caption("Из разборов писем клиентов и подборов по справочнику — без текста писем и данных клиентов: вид "
               "продукции, требования, итог по позиции. Проверочные прогоны не учитываются. Причину проигрыша "
               "менеджер выбирает при смене статуса ТКП на «проигран» в «Реестре ТКП».")
    period = st.segmented_control("Период", list(PERIODS), default="90 дней", key="demand-period",
                                  label_visibility="collapsed") or "90 дней"
    days = PERIODS[period]
    since = dt.date.today() - dt.timedelta(days=days) if days else None
    s = demand.summary(demand.load(since), tkp_registry.load(), since=since)
    if not s["positions"] and not (s["won"] or s["lost"]):
        st.info("За этот период запросов нет. Спрос копится сам: письма клиентов («Черновик ТКП», входящие из почты, "
                "API) и подборы по справочнику («осциллограф от 150 МГц и 4 канала»).")
        return
    c = st.columns(4)
    c[0].metric("Запросов", s["requests"], help="Писем клиентов и подборов по справочнику")
    c[1].metric("Позиций", s["positions"])
    c[2].metric("Закрыто", f"{s['closed_share']} %" if s["closed_share"] is not None else "—",
                help="Позиции, по которым предложена модель из справочника")
    decided = s["won"] + s["lost"]
    c[3].metric("ТКП выиграно", f"{s['won']} из {decided}" if decided else "—",
                help="Из ТКП со статусом «выигран» или «проигран»")
    left, right = st.columns(2)
    with left:
        st.markdown("**Что запрашивают**")
        if s["kinds"]:
            st.dataframe(pd.DataFrame([{"Вид продукции": k, "Запросов": n, "Штук": qty,
                                        "Закрыто": round(100 * closed / n)} for k, n, qty, closed in s["kinds"]]),
                         hide_index=True, width="stretch", column_config={
                             "Закрыто": st.column_config.ProgressColumn("Закрыто", format="%d %%", min_value=0,
                                                                        max_value=100)})
    with right:
        st.markdown("**Что предлагаем чаще всего**")
        if s["offered"]:
            st.bar_chart(pd.DataFrame(s["offered"], columns=["Модель", "Штук"]), x="Модель", y="Штук",
                         horizontal=True, sort="-Штук", color="#6B4FA0", height=min(60 + 34 * len(s["offered"]), 380))
        else:
            st.caption("Пока ни одна модель не предложена.")
        if s["asked"]:
            st.caption("Спрашивали по названию: " + ", ".join(f"{m} ({n})" for m, n in s["asked"]))
    st.markdown("**Не закрыто — что дописать в справочник или добавить в ассортимент**")
    if s["unmet"]:
        st.dataframe(pd.DataFrame([{"Вид продукции": k, "Почему не закрыто": why, "Раз": n}
                                   for k, why, n in s["unmet"]]), hide_index=True, width="stretch")
    else:
        st.caption("Все позиции закрыты моделями из справочника.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Частые требования клиентов**")
        if s["reqs"]:
            st.dataframe(pd.DataFrame(s["reqs"], columns=["Требование", "Раз"]), hide_index=True, width="stretch")
    with right:
        st.markdown("**Проигрыши: причины**")
        if s["lost_reasons"]:
            st.dataframe(pd.DataFrame(s["lost_reasons"], columns=["Причина", "ТКП"]), hide_index=True,
                         width="stretch")
            if s["by_model"]:
                st.dataframe(pd.DataFrame(s["by_model"], columns=["Модель", "Выиграно", "Проиграно"]),
                             hide_index=True, width="stretch")
        else:
            st.caption("Проигранных ТКП нет.")


def users_tab() -> None:
    """Учётные записи: создать, роль, блокировка, сброс пароля, удаление — временный пароль показывается один раз."""
    from kbagent import users
    st.caption("Новый пользователь и сброс пароля — временный пароль: передайте его лично; при первом входе "
               "пользователь задаст свой. Заблокированный выходит из системы сразу. Действия — в журнале «Безопасность».")
    if note := st.session_state.pop("users_note", None):
        getattr(st, note[0])(note[1])
    if temp := st.session_state.pop("users_temp", None):
        st.success(f"Временный пароль для «{temp[0]}» — показывается один раз:")
        st.code(temp[1], language=None)
    people = users.listing()
    if people:
        st.dataframe(pd.DataFrame([{"Логин": u["login"], "Имя": u["name"], "Роль": u["role"],
                                    "Статус": "заблокирован" if not u["active"] else (
                                        "ждёт смены пароля" if u["must_change"] else "активен"),
                                    "Создан": u.get("created", "—")} for u in people]),
                     hide_index=True, width="stretch")
    with st.form("user-add", clear_on_submit=True):
        st.markdown("**Новый пользователь**")
        c1, c2, c3 = st.columns(3)
        login = c1.text_input("Логин", placeholder="ivanov")
        name = c2.text_input("Имя", placeholder="Иванов И. И., инженер техпродаж")
        role = c3.selectbox("Роль", list(settings.roles))
        if st.form_submit_button("Создать", type="primary"):
            try:
                st.session_state.users_temp = (login.strip().lower(), users.add(login, name, role, ctx()["login"]))
            except users.UserError as e:
                st.session_state.users_note = ("error", str(e))
            st.rerun()
    if not people:
        return
    with st.container(border=True):
        st.markdown("**Изменить пользователя**")
        login = st.selectbox("Пользователь", [u["login"] for u in people], key="user-pick",
                             format_func=lambda x: next(f"{u['login']} — {u['name']}" for u in people if u["login"] == x))
        u = next(x for x in people if x["login"] == login)
        c1, c2 = st.columns(2)
        role = c1.selectbox("Роль", list(settings.roles), index=list(settings.roles).index(u["role"]),
                            key=f"user-role-{login}")
        active = c2.toggle("Доступ разрешён", value=u["active"], key=f"user-active-{login}")
        b1, b2, b3 = st.columns(3)
        try:
            if b1.button("Сохранить", key=f"user-save-{login}", type="primary"):
                users.update(login, ctx()["login"], role=role, active=active)
                st.session_state.users_note = ("success", f"«{login}» сохранён.")
                st.rerun()
            if b2.button("Сбросить пароль", key=f"user-reset-{login}"):
                st.session_state.users_temp = (login, users.reset_password(login, ctx()["login"]))
                st.rerun()
            sure = b3.checkbox("Удалить — подтверждаю", key=f"user-sure-{login}")
            if sure and b3.button("Удалить", key=f"user-del-{login}"):
                users.remove(login, ctx()["login"])
                st.session_state.users_note = ("success", f"«{login}» удалён.")
                st.rerun()
        except users.UserError as e:
            st.error(str(e))


def acks_tab() -> None:
    """Лист ознакомления: кто из сотрудников, кому документ доступен по роли, отметил «Ознакомлен» в чате."""
    from kbagent import changes, users
    from kbagent.export import ack_docx
    st.caption("Новые редакции и документы, обновлённые администратором: сотрудники получают уведомление в чате и "
               "отмечают «Ознакомлен». Здесь — кто ознакомился и кто нет; лист ознакомления в Word — для архива СМК "
               "и аудита. Не ознакомившимся уведомление показывается при каждом входе.")
    events = changes.ack_events()
    if not events:
        st.info("За последний год новых редакций, требующих ознакомления, не было. Они появятся после утверждения "
                "новой редакции на вкладке «Документы».")
        return
    people = users.listing()
    for e in events[:30]:
        rows = changes.sheet(e, people)
        done = sum(1 for r in rows if r["at"])
        with st.expander(f"{changes.KINDS[e['kind']]}: {e['title']} — {display(e['time'])[:10]} · "
                         f"ознакомились {done} из {len(rows)}", expanded=done < len(rows) and e is events[0]):
            if e.get("old_title"):
                st.caption(f"Вместо «{e['old_title']}»" + (f"; {e['summary']}" if e.get("summary") else ""))
            st.progress(done / len(rows) if rows else 1.0)
            if rows:
                st.dataframe(pd.DataFrame([{"Сотрудник": r["name"], "Роль": r["role"],
                                            "Ознакомлен": display(r["at"])[:16] if r["at"] else "—"} for r in rows]),
                             hide_index=True, width="stretch")
            st.download_button("Лист ознакомления (Word)",
                               ack_docx({**e, "kind_label": changes.KINDS[e["kind"]]}, rows, APP_NAME),
                               file_name=f"oznakomlenie_{e['doc_id']}.docx", icon=":material/download:",
                               key=f"ack-docx-{e['id']}",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def admin_page() -> None:
    inject_css()
    if not ctx()["is_admin"]:
        st.error("Раздел доступен только администратору.")
        return
    st.markdown("### Администрирование")
    from kbagent import users
    if demo := users.demo_accounts():
        st.error("Активны демо-учётки с паролями из открытого репозитория: " + ", ".join(demo) + ". На установке "
                 "у заказчика удалите их или сбросьте пароли на вкладке «Пользователи» — или выполните "
                 "scripts\\first_run.py.", icon=":material/gpp_bad:")
    if late := jobs.warnings(jobs.status()):
        st.warning("Фоновые задачи: " + "; ".join(late) + ". Подробнее — вкладка «Система».",
                   icon=":material/schedule:")
    tabs = st.tabs(["Эффект", "Спрос", "Журнал запросов", "Пробелы в документации", "Оценки", "Документы",
                    "Ознакомление", "Справочник", "Вопросы эксперту", "Качество", "Внедрение", "Пользователи",
                    "Безопасность", "Система"])
    for tab, show in zip(tabs, (effect_tab, demand_tab, journal, gaps, feedback, docs, acks_tab, reference,
                                expert_queue, quality, rollout_tab, users_tab, security, system)):
        with tab:
            show()

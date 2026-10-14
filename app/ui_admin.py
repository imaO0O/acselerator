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
from kbagent.ingest import ocr_available, registry_info
from kbagent.uploads import (UPLOAD_TYPES, add_upload, approve_upload, documents, list_uploads, remove_upload,
                             similar_documents)
from kbagent import effect, experts, prais, spravochnik
from kbagent.actions import log_action
from kbagent.text import md_plain

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
        st.dataframe(df, hide_index=True, use_container_width=True,
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
                         hide_index=True, use_container_width=True,
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
    st.dataframe(df, hide_index=True, use_container_width=True)
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
                     hide_index=True, use_container_width=True)
    if rep.contradictions:
        st.info("Источники расходятся — агент показывает оба значения и передаёт вопрос эксперту:")
        st.dataframe(pd.DataFrame([{"Модель": x["model"], "Параметр": x["param"],
                                    "Значения": "; ".join(f"{v['value']} (с. {v['page']})" for v in x["values"])}
                                   for x in rep.contradictions]), hide_index=True, use_container_width=True)
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
        st.dataframe(df, hide_index=True, use_container_width=True, height=360)
        st.download_button("Скачать справочник (CSV)", spravochnik.PATH.read_bytes(), file_name="spravochnik.csv",
                           icon=":material/download:", key="spr-dl")
    price_list()


def price_list() -> None:
    """Прайс для ТКП: цена и срок поставки моделей справочника; в ТКП — с пометкой «по прайсу от …»."""
    prices, rep = prais.load()
    st.subheader("Прайс для ТКП")
    st.caption("Цена за штуку и срок поставки моделей из справочника параметров. В ТКП цена и сумма подставляются "
               "с пометкой «по прайсу от …»; чего нет в прайсе — вписывает менеджер. Файл — CSV или XLSX с "
               "колонками: " + ", ".join(prais.COLUMNS) + ".")
    c = st.columns(3)
    c[0].metric("Моделей с ценой", rep.prices)
    c[1].metric("Строк с ошибками", len(rep.errors) if prices else 0)
    c[2].metric("Дата прайса", rep.date or "—")
    if rep.demo:
        st.info("Сейчас загружен демонстрационный прайс: цены условные, в ТКП они помечены «ДЕМО». Загрузите прайс "
                "предприятия — пометка исчезнет.")
    if prices and rep.errors:
        st.warning("Строки прайса, не прошедшие проверку, не загружены:")
        st.dataframe(pd.DataFrame(rep.errors).rename(columns={"line": "Строка файла", "error": "Причина"}),
                     hide_index=True, use_container_width=True)
    with st.container(border=True):
        st.markdown("**Загрузить новый прайс**")
        st.caption("Проверяется до замены: цена — положительное число, модель есть в справочнике, повторы. Если ни "
                   "одна строка не прошла проверку, действующий прайс остаётся. Дата прайса — день загрузки.")
        st.session_state.setdefault("price_key", 0)
        f = st.file_uploader("Прайс", type=["csv", "xlsx"], key=f"price-{st.session_state.price_key}",
                             label_visibility="collapsed")
        if f is not None:
            try:
                new = prais.replace_file(f.name, f.getvalue())
                log_action("prais", ctx()["login"], f.name, f"цен: {new.prices}; ошибок: {len(new.errors)}")
                st.session_state.price_note = ("success", f"Загружено цен: {new.prices}. Ошибок: {len(new.errors)}.")
            except ValueError as e:
                st.session_state.price_note = ("error", str(e))
            st.session_state.price_key += 1
            st.rerun()
        if note := st.session_state.pop("price_note", None):
            getattr(st, note[0])(note[1])
    if prices:
        st.dataframe(pd.DataFrame([{"Модель": p.model, "Цена, ₽": prais.money(p.price), "Срок поставки": p.lead or "—"}
                                   for p in prices.values()]), hide_index=True, use_container_width=True, height=280)


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
            st.markdown(f"**{md_plain(it['subject'])}** · {it['kind']}  \n:gray[{display(it['time'])[:16]} · обращений: "
                        f"{it.get('asked', 1)} · {it['status']}]")
            st.caption(details if isinstance(it["details"], str) else f"Значения: {details}")
            if it.get("question") and it["question"] != it["subject"]:
                st.caption(f"Вопрос сотрудника: «{md_plain(it['question'])}»")
            if it.get("comment"):
                st.caption(f"Что вызывает сомнение: {md_plain(it['comment'])}")
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
                                   for r in reversed(stale)]), hide_index=True, use_container_width=True)
    if records:
        st.markdown("**Утверждённые ответы и ТКП**")
        st.dataframe(pd.DataFrame([{"Когда": display(r["time"])[:16], "Что": r["kind"], "Сотрудник": r["user"],
                                    "Вопрос": r["question"][:90],
                                    "От вопроса до утверждения": fmt(r["seconds"] / 60),
                                    "Позиций": r.get("positions") or "—"} for r in reversed(records)]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("Пока ничего не утверждено: кнопка «Черновик» или «Черновик ТКП» под ответом в чате → "
                "«Утвердить и скачать».")


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
    tabs = st.tabs(["Эффект", "Журнал запросов", "Пробелы в документации", "Оценки", "Документы", "Справочник",
                    "Вопросы эксперту", "Качество", "Безопасность", "Система"])
    with tabs[0]:
        effect_tab()
    with tabs[1]:
        journal()
    with tabs[2]:
        gaps()
    with tabs[3]:
        feedback()
    with tabs[4]:
        docs()
    with tabs[5]:
        reference()
    with tabs[6]:
        expert_queue()
    with tabs[7]:
        quality()
    with tabs[8]:
        security()
    with tabs[9]:
        system()

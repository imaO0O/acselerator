"""Страница «Реестр ТКП»: номера, статусы и файлы утверждённых предложений; бланк и условия компании."""
import pandas as pd
import streamlit as st

from app.ui_common import ctx, get_agent, inject_css
from kbagent import mailbox, prajs, tkp_registry
from kbagent.actions import log_action
from kbagent.config import settings

STATUS_ICONS = {"утверждён": "🟣", "отправлен клиенту": "🔵", "выигран": "🟢", "проигран": "⚪", "отменён": "⚫",
                "заменён": "◽"}
ATTENTION_ICONS = {"expired": ":material/event_busy:", "expiring": ":material/schedule:",
                   "silent": ":material/notifications:", "not_sent": ":material/outgoing_mail:"}


def _money(x) -> str:
    return prajs.money(x) if x else "—"


def _date(value: str) -> str:
    """«2026-10-26 15:30» → «26.10.2026»."""
    y, m, d = value[:10].split("-")
    return f"{d}.{m}.{y}"


def _incoming() -> None:
    """Входящие запросы из почтового ящика отдела: разбор готов, менеджер открывает его в чате."""
    with st.container(border=True):
        st.markdown("**Входящие запросы из почты**")
        if not mailbox.configured():
            st.caption("Почтовый ящик отдела не подключён. Укажите KB_MAIL_HOST, KB_MAIL_USER и пароль "
                       "(KB_MAIL_PASSWORD или data/mail_password.txt): письма с запросами на продукцию будут "
                       "появляться здесь с готовым разбором. Ящик читается только на чтение — письма не помечаются "
                       "прочитанными, ничего не отправляется.")
            return
        st.caption(f"Ящик {settings.mail_user}, папка {settings.mail_folder} — только чтение. Автоматически — "
                   "scripts/check_mail.py --watch 300.")
        if st.button("Проверить почту", icon=":material/mail:", key="mail-check"):
            with st.spinner("Читаю новые письма…"):
                rep = mailbox.check()
            st.session_state.mail_note = ("error", rep["error"]) if rep["error"] else (
                "success", f"Новых писем: {rep['checked']}, из них запросов клиента: {rep['requests']}.")
            st.rerun()
        if note := st.session_state.pop("mail_note", None):
            getattr(st, note[0])(note[1])
        new = [m for m in reversed(mailbox.load()) if m["status"] == "новый"]
        if not new:
            st.caption("Новых запросов нет.")
        for m in new[:20]:
            c1, c2, c3 = st.columns([6, 2, 2], vertical_alignment="center")
            c1.markdown(f"**{m['subject'] or 'без темы'}** · {m['sender_name'] or m['sender']}  \n"
                        f":gray[{m['received'] or m['created']} · {'; '.join(m['positions'])}"
                        + (f" · вложения: {', '.join(m['attachments'])}" if m["attachments"] else "") + "]")
            if c2.button("Открыть в чате", key=f"mail-open-{m['id']}", type="primary", width="stretch"):
                _open(m)
            if c3.button("Не запрос", key=f"mail-skip-{m['id']}", type="tertiary", width="stretch"):
                mailbox.set_status(m["id"], "не запрос", ctx()["login"], ctx()["name"])
                st.rerun()


def _open(m: dict) -> None:
    from app.ui_chat import open_in_chat
    role = ctx()["role"]
    r = get_agent().ask(f"Подготовь ТКП по письму клиента:\n{m['letter']}", role=role, user=ctx()["login"])
    open_in_chat(f"Письмо клиента: {m['subject'] or 'без темы'} · {m['sender']}", r, role)
    mailbox.set_status(m["id"], "открыт", ctx()["login"], ctx()["name"])
    if page := st.session_state.get("chat_page"):
        st.switch_page(page)
    st.rerun()


def registry_page() -> None:
    inject_css()
    st.markdown("### Реестр ТКП")
    _incoming()
    records = tkp_registry.load()
    s = tkp_registry.summary(records)
    c = st.columns(5)
    c[0].metric("ТКП", s["count"])
    c[1].metric("На сумму", _money(s["total"]))
    c[2].metric("В работе", s["open"], help="утверждены или отправлены, решения клиента ещё нет")
    c[3].metric("Выиграно", s["won"], help=_money(s["won_total"]))
    c[4].metric("Доля выигранных", f"{s['win_rate']} %" if s["win_rate"] is not None else "—",
                help="среди ТКП с решением клиента: выигран или проигран")
    _attention(records)
    if not records:
        st.info("Утверждённых ТКП пока нет. ТКП попадает сюда с номером, когда его утверждают в окне "
                "«Черновик ТКП» в чате.")
    else:
        f1, f2 = st.columns([2, 3])
        statuses = f1.multiselect("Статус", tkp_registry.STATUSES, placeholder="все статусы", key="tkp-st")
        query = f2.text_input("Найти", placeholder="номер, клиент или прибор", key="tkp-q").strip().lower()
        shown = [r for r in reversed(records) if (not statuses or r["status"] in statuses) and (
            not query or query in f"{r['number']} {r['client']} {' '.join(r['models'])}".lower())]
        st.dataframe(pd.DataFrame([{"Номер": r["number"], "Дата": _date(r["date"]), "Клиент": r["client"] or "—",
                                    "Приборы": ", ".join(r["models"]) or "—", "Сумма": _money(r["total"]),
                                    "Статус": f"{STATUS_ICONS.get(r['status'], '')} {r['status']}",
                                    "Утвердил": r["approved_by"], "Действует до": r["valid_until"] or "—"}
                                   for r in shown]), hide_index=True, width="stretch", height=min(420, 35 * (len(shown) + 1) + 3))
        st.download_button("Реестр в Excel", tkp_registry.export(records), file_name="reestr_tkp.xlsx",
                           icon=":material/download:", key="tkp-export",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if shown:
            _record([r["number"] for r in shown])
    if ctx()["is_admin"]:
        _settings()


def _attention(records: list[dict]) -> None:
    """ТКП, по которым нужно действие менеджера: срок истекает или истёк, клиент молчит, ТКП не отправлено."""
    grouped: dict[str, list[dict]] = {}                    # одно ТКП — одна строка, причины через «;»
    for a in tkp_registry.attention(records):
        grouped.setdefault(a["record"]["number"], []).append(a)
    if not grouped:
        return
    conf = tkp_registry.load_settings()
    with st.container(border=True):
        st.markdown(f"**Требуют внимания: {len(grouped)}**")
        st.caption(f"Напоминание — если клиент молчит {conf['follow_up_days']} дн. после отправки или до конца срока "
                   f"действия осталось {conf['expiring_days']} дн. и меньше. Сроки меняет администратор в условиях ТКП.")
        for k, (number, items) in enumerate(grouped.items()):
            r, kinds = items[0]["record"], [a["kind"] for a in items]
            c1, c2, c3 = st.columns([6, 2, 2], vertical_alignment="center")
            c1.markdown(f"**{number}** · {r['client'] or 'клиент не указан'} · {_money(r['total'])}  \n"
                        f":gray[{'; '.join(a['text'] for a in items)}]")
            if kinds == ["not_sent"]:
                if c2.button("Отправлен клиенту", key=f"att-sent-{k}", width="stretch",
                             icon=":material/outgoing_mail:"):
                    tkp_registry.set_status(number, "отправлен клиенту", ctx()["login"], ctx()["name"])
                    st.rerun()
            elif c2.button("Напоминание", key=f"att-rem-{k}", width="stretch", type="primary",
                           icon=ATTENTION_ICONS[kinds[0]]):
                reminder_dialog(number)
            if {"expired", "expiring"} & set(kinds) and c3.button(
                    "Новая редакция", key=f"att-rev-{k}", width="stretch", icon=":material/history:",
                    help="Обновить ТКП с текущими ценами и сроками: номер …/1, прежняя версия — «заменён»"):
                revision_dialog(number)


@st.dialog("Напоминание клиенту", width="large")
def reminder_dialog(number: str) -> None:
    """Черновик письма-напоминания. Ничего не отправляется: менеджер копирует текст в свою почту
    и отмечает «напомнили» — отсчёт молчания клиента начнётся заново."""
    r = next(x for x in tkp_registry.load() if x["number"] == number)
    st.caption(f"ТКП № {number} · {r['client'] or 'клиент не указан'}. Скопируйте текст в письмо клиенту — "
               "автоматически ничего не отправляется.")
    st.text_area("Текст", tkp_registry.reminder_text(r, ctx()["name"]), height=260, key=f"rem-text-{number}",
                 label_visibility="collapsed")
    if st.button("Отметить: напомнили клиенту", type="primary", icon=":material/task_alt:", key=f"rem-ok-{number}"):
        tkp_registry.note(number, "напомнили клиенту", ctx()["login"], ctx()["name"])
        st.rerun()


@st.dialog("Новая редакция ТКП", width="large")
def revision_dialog(number: str) -> None:
    """Клиент поменял прибор или количество, или срок действия истёк: запрос правится, разбор открывается в чате;
    после утверждения — номер «…/1», прежняя версия в реестре — «заменён», история сохраняется."""
    r = next(x for x in tkp_registry.load() if x["number"] == number)
    data = tkp_registry.tkp_data(number) or {}
    st.caption(f"ТКП № {number}. Поправьте запрос клиента — количество, требования или приборы; разбор откроется в "
               "чате, там же можно выбрать другой подходящий прибор. После утверждения новая редакция получит "
               f"номер {number}/1 (или следующий), а эта версия станет «заменён».")
    letter = st.text_area("Запрос клиента", data.get("letter") or r["question"], height=220, key=f"rev-text-{number}")
    if st.button("Разобрать и открыть в чате", type="primary", icon=":material/arrow_forward:",
                 key=f"rev-ok-{number}", disabled=not letter.strip()):
        from app.ui_chat import open_in_chat
        role = ctx()["role"]
        res = get_agent().ask(f"Подготовь ТКП по письму клиента:\n{letter.strip()}", role=role, user=ctx()["login"],
                              count_demand=False)        # тот же запрос клиента — в спрос второй раз не идёт
        open_in_chat(f"Новая редакция ТКП № {number}", res, role, tkp_revision_of=number)
        if page := st.session_state.get("chat_page"):
            st.switch_page(page)
        st.rerun()


def _record(numbers: list[str]) -> None:
    with st.container(border=True):
        number = st.selectbox("ТКП", numbers, key="tkp-pick")
        r = next(x for x in tkp_registry.load() if x["number"] == number)
        st.markdown(f"**{r['number']}** · {r['client'] or 'клиент не указан'} · {_money(r['total'])} · "
                    f"{STATUS_ICONS.get(r['status'], '')} {r['status']}")
        st.caption(f"Запрос: {r['question']}")
        newer = [x["number"] for x in tkp_registry.load() if x.get("revision_of") == number]
        if r.get("revision_of") or newer:
            st.caption(("Редакция ТКП № " + r["revision_of"] if r.get("revision_of") else "")
                       + (" · " if r.get("revision_of") and newer else "")
                       + ("Заменён редакцией " + ", ".join(newer) if newer else ""))
        docx_path, xlsx_path = tkp_registry.files(number)
        b1, b2 = st.columns(2)
        if docx_path.exists():
            b1.download_button("ТКП в Word", docx_path.read_bytes(), file_name=docx_path.name, key="tkp-docx",
                               icon=":material/description:", width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if xlsx_path.exists():
            b2.download_button("ТКП в Excel", xlsx_path.read_bytes(), file_name=xlsx_path.name, key="tkp-xlsx",
                               icon=":material/table:", width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s1, s2, s3 = st.columns([2, 3, 1], vertical_alignment="bottom")
        status = s1.selectbox("Статус", tkp_registry.STATUSES, index=tkp_registry.STATUSES.index(r["status"]),
                              key=f"tkp-status-{number}")
        lost = status == "проигран" and r["status"] != "проигран"
        comment = s2.text_input("Подробности" if lost else "Комментарий",
                                placeholder="например, у конкурента дешевле на 12 %" if lost
                                else "например, «клиент просит скидку»", key=f"tkp-comment-{number}")
        reason = st.pills("Причина проигрыша — попадёт в аналитику спроса", tkp_registry.LOST_REASONS,
                          key=f"tkp-reason-{number}") if lost else None
        if s3.button("Сохранить", key=f"tkp-save-{number}", disabled=status == r["status"] and not comment.strip()):
            text = "; ".join(x for x in (reason, comment.strip()) if x)
            tkp_registry.set_status(number, status, ctx()["login"], ctx()["name"], text)
            st.rerun()
        if r["status"] not in ("заменён", "отменён") and st.button(
                "Новая редакция", key=f"tkp-rev-{number}", icon=":material/history:",
                help="Клиент поменял прибор или количество: номер …/1, прежняя версия — «заменён», история сохраняется"):
            revision_dialog(number)
        st.caption("История: " + "; ".join(f"{h['at']} — {h['status']} ({h['by']})" + (f": {h['comment']}"
                                                                                      if h["comment"] else "")
                                           for h in r["history"]))


def _settings() -> None:
    with st.expander("Бланк и условия ТКП (администратор)", icon=":material/settings:"):
        conf = tkp_registry.load_settings()
        st.caption("Бланк — документ Word с шапкой, реквизитами, логотипом и колонтитулами. ТКП встаёт на место "
                   "метки {{ТКП}} (иначе — после текста бланка); метки {{номер}}, {{дата}}, {{действует_до}} "
                   "заполняются, в том числе в колонтитулах.")
        st.session_state.setdefault("blank_key", 0)
        f = st.file_uploader("Бланк компании (.docx)", type=["docx"], key=f"blank-{st.session_state.blank_key}")
        if f is not None:
            try:
                hint = tkp_registry.save_blank(f.getvalue())
                log_action("tkp_settings", ctx()["login"], f.name, "загружен бланк")
                st.session_state.blank_note = ("success", f"Бланк загружен. {hint}")
            except ValueError as e:
                st.session_state.blank_note = ("error", str(e))
            st.session_state.blank_key += 1
            st.rerun()
        if note := st.session_state.pop("blank_note", None):
            getattr(st, note[0])(note[1])
        if tkp_registry.blank():
            st.caption("Бланк загружен.")
        with st.form("tkp-settings"):
            company = st.text_input("Компания (в шапке Excel)", conf["company"])
            a, b = st.columns(2)
            prefix = a.text_input("Префикс номера", conf["prefix"], help="номер — префикс, год и порядковый: ТКП-2026-0001")
            days = b.number_input("Срок действия, дней", 0, 365, int(conf["validity_days"]),
                                  help="0 — без срока действия")
            c, d = st.columns(2)
            follow = c.number_input("Напомнить, если клиент молчит, дней", 0, 90, int(conf["follow_up_days"]),
                                    help="0 — не напоминать")
            expiring = d.number_input("Предупредить до конца срока, дней", 0, 60, int(conf["expiring_days"]))
            payment = st.text_input("Условия оплаты", conf["payment"], placeholder="например, 100 % предоплата по счёту")
            delivery = st.text_input("Условия поставки", conf["delivery"],
                                     placeholder="например, самовывоз со склада в Рязани")
            if st.form_submit_button("Сохранить условия", type="primary"):
                tkp_registry.save_settings({"company": company, "prefix": prefix, "validity_days": days,
                                            "payment": payment, "delivery": delivery,
                                            "follow_up_days": follow, "expiring_days": expiring})
                log_action("tkp_settings", ctx()["login"], "условия ТКП", f"префикс {prefix}; срок {days} дн.")
                st.success("Сохранено — действует для следующих ТКП.")

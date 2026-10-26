"""Страница «Реестр ТКП»: номера, статусы и файлы утверждённых предложений; бланк и условия компании."""
import pandas as pd
import streamlit as st

from app.ui_common import ctx, inject_css
from kbagent import prajs, tkp_registry
from kbagent.actions import log_action

STATUS_ICONS = {"утверждён": "🟣", "отправлен клиенту": "🔵", "выигран": "🟢", "проигран": "⚪", "отменён": "⚫"}


def _money(x) -> str:
    return prajs.money(x) if x else "—"


def _date(value: str) -> str:
    """«2026-10-26 15:30» → «26.10.2026»."""
    y, m, d = value[:10].split("-")
    return f"{d}.{m}.{y}"


def registry_page() -> None:
    inject_css()
    st.markdown("### Реестр ТКП")
    records = tkp_registry.load()
    s = tkp_registry.summary(records)
    c = st.columns(5)
    c[0].metric("ТКП", s["count"])
    c[1].metric("На сумму", _money(s["total"]))
    c[2].metric("В работе", s["open"], help="утверждены или отправлены, решения клиента ещё нет")
    c[3].metric("Выиграно", s["won"], help=_money(s["won_total"]))
    c[4].metric("Доля выигранных", f"{s['win_rate']} %" if s["win_rate"] is not None else "—",
                help="среди ТКП с решением клиента: выигран или проигран")
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


def _record(numbers: list[str]) -> None:
    with st.container(border=True):
        number = st.selectbox("ТКП", numbers, key="tkp-pick")
        r = next(x for x in tkp_registry.load() if x["number"] == number)
        st.markdown(f"**{r['number']}** · {r['client'] or 'клиент не указан'} · {_money(r['total'])} · "
                    f"{STATUS_ICONS.get(r['status'], '')} {r['status']}")
        st.caption(f"Запрос: {r['question']}")
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
        comment = s2.text_input("Комментарий", placeholder="например, «выбрали конкурента — цена»",
                                key=f"tkp-comment-{number}")
        if s3.button("Сохранить", key=f"tkp-save-{number}", disabled=status == r["status"] and not comment.strip()):
            tkp_registry.set_status(number, status, ctx()["login"], ctx()["name"], comment)
            st.rerun()
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
            payment = st.text_input("Условия оплаты", conf["payment"], placeholder="например, 100 % предоплата по счёту")
            delivery = st.text_input("Условия поставки", conf["delivery"],
                                     placeholder="например, самовывоз со склада в Рязани")
            if st.form_submit_button("Сохранить условия", type="primary"):
                tkp_registry.save_settings({"company": company, "prefix": prefix, "validity_days": days,
                                            "payment": payment, "delivery": delivery})
                log_action("tkp_settings", ctx()["login"], "условия ТКП", f"префикс {prefix}; срок {days} дн.")
                st.success("Сохранено — действует для следующих ТКП.")

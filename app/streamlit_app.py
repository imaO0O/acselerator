"""Агент по документам и учёту предприятия — веб-интерфейс.

    python scripts/run_chat.py        (или scripts\\run_chat.bat)

Вход по логину и паролю; KB_AUTH=0 — выбор роли из списка (для отладки).
Страницы: «Чат» (app/ui_chat.py) и для администратора «Администрирование» (app/ui_admin.py).
"""
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui_admin import admin_page  # noqa: E402
from app.ui_chat import chat_page  # noqa: E402
from app.ui_common import APP_NAME, LOGO, inject_css  # noqa: E402
from app.ui_tkp import registry_page  # noqa: E402
from kbagent import auth, users  # noqa: E402
from kbagent.actions import log_action  # noqa: E402
from kbagent.config import settings  # noqa: E402

COOKIE = "kb_session"

st.set_page_config(page_title=APP_NAME, page_icon=LOGO, layout="wide")
inject_css()
st.logo(LOGO, size="large")


def set_cookie(value: str, max_age: int) -> None:
    """Сеанс переживает перезагрузку страницы: в браузере хранится только случайный токен."""
    components.html(f"<script>parent.document.cookie = '{COOKIE}={value}; path=/; max-age={max_age}; "
                    "SameSite=Strict';</script>", height=0)


def current_user() -> dict | None:
    if "user" in st.session_state:
        # роль и блокировка проверяются при каждом обновлении: заблокированного выкидывает сразу, а не через 12 часов
        fresh = auth.session_user(st.session_state.get("token"))
        if fresh is None:
            for k in ("user", "token", "conv", "ctx"):
                st.session_state.pop(k, None)
            st.session_state.update(logged_out=True, clear_cookie=True)
            return None
        st.session_state.user = fresh
        return fresh
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
    _, col, _ = st.columns([1, 1.3, 1])
    with col:
        st.write("")
        st.image(LOGO, width=56)
        st.markdown(f"### {APP_NAME}")
        st.caption("Ответы по внутренним документам и данным учёта предприятия со ссылкой на источник.")
        with st.form("login"):
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            submitted = st.form_submit_button("Войти", type="primary", width="stretch")
        if submitted:
            user = auth.authenticate(login, password)
            locked = auth.locked_for(login)
            log_action("login" if user else "login_locked" if locked else "login_failed", login.strip() or "—")
            if user:
                token = auth.create_session(user)
                st.session_state.update(user=user, token=token, set_cookie=token, logged_out=False)
                st.session_state.pop("conv", None)
                st.rerun()
            if locked:
                st.error(f"Слишком много неудачных попыток. Вход под этим логином закрыт ещё на "
                         f"{max(1, round(locked / 60))} мин.")
            else:
                st.error("Неверный логин или пароль.")
        st.caption("Нет доступа? Обратитесь к администратору системы.")
    st.stop()


def password_form(login: str, first: bool) -> None:
    """Смена пароля: при первом входе с временным паролем — обязательно, потом — по кнопке в боковой панели."""
    with st.form(f"pwd-{login}-{'first' if first else 'side'}"):
        old = st.text_input("Текущий (временный) пароль" if first else "Текущий пароль", type="password")
        new = st.text_input(f"Новый пароль — не короче {users.MIN_LENGTH} символов, буквы и цифры", type="password")
        again = st.text_input("Новый пароль ещё раз", type="password")
        if st.form_submit_button("Сменить пароль", type="primary"):
            if new != again:
                st.error("Пароли не совпадают.")
            else:
                try:
                    users.change_password(login, old, new)
                    st.session_state.user["must_change"] = False
                    st.success("Пароль сменён.")
                    st.rerun()
                except users.UserError as e:
                    st.error(str(e)[:1].upper() + str(e)[1:] + ".")


if settings.auth_required:
    user = current_user()
    if user is None:
        login_page()
    if user.get("must_change"):
        _, col, _ = st.columns([1, 1.3, 1])
        with col:
            st.markdown("### Задайте свой пароль")
            st.caption("Вы вошли с временным паролем от администратора. Чтобы продолжить, задайте свой.")
            password_form(user["login"], first=True)
        st.stop()
    if token := st.session_state.pop("set_cookie", None):
        set_cookie(token, auth.SESSION_TTL)
    st.session_state.ctx = {"login": user["login"], "name": user["name"], "role": user["role"],
                            "is_admin": user["role"] == "Администратор", "history_key": user["login"]}
else:
    with st.sidebar:
        role = st.selectbox("Роль (режим отладки без входа)", list(settings.roles), index=len(settings.roles) - 1)
    st.session_state.ctx = {"login": None, "name": "Отладка", "role": role, "is_admin": role == "Администратор",
                            "history_key": "_debug"}

ctx = st.session_state.ctx
pages = [st.Page(chat_page, title="Чат", icon=":material/forum:", default=True)]
st.session_state.chat_page = pages[0]          # «Открыть в чате» из реестра ТКП переходит сюда
if ctx["role"] in settings.tkp_roles:
    pages.append(st.Page(registry_page, title="Реестр ТКП", icon=":material/request_quote:", url_path="tkp"))
if ctx["is_admin"]:
    pages.append(st.Page(admin_page, title="Администрирование", icon=":material/admin_panel_settings:",
                         url_path="admin"))
st.navigation(pages, position="sidebar" if len(pages) > 1 else "hidden").run()

with st.sidebar:
    st.divider()
    st.markdown(f"**{ctx['name']}**  \n:gray[{ctx['role']}]")
    if settings.auth_required:
        with st.popover("Сменить пароль", icon=":material/key:"):
            password_form(ctx["login"], first=False)
    if settings.auth_required and st.button("Выйти", icon=":material/logout:", key="logout", type="tertiary"):
        log_action("logout", ctx["login"])
        auth.end_session(st.session_state.get("token"))
        for k in ("user", "token", "conv", "ctx"):
            st.session_state.pop(k, None)
        st.session_state.update(logged_out=True, clear_cookie=True)
        st.rerun()

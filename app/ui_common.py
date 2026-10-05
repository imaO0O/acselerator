"""Общее для страниц чата и администрирования: оформление, подписи, кэшируемые ресурсы."""
import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kbagent import llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.preview import render_page  # noqa: E402

APP_NAME = "Агент по документам и учёту"
LOGO = str(ROOT / "app" / "static" / "logo.svg")
USER_AVATAR = ":material/person:"

ACCESS_LABELS = {"all": "Все сотрудники", "production": "Производство", "metrology": "Метрология",
                 "admin": "Только администратор", "archive": "Архив (отменён)"}
ROUTE_LABELS = {"documents": "Документы", "database": "Учётная база", "both": "База и документы",
                "help": "Справка", "normo": "Нормоконтроль"}
STATUS_LABELS = {"answer": "Ответ", "no_data": "Нет в источниках", "model_refused": "Нет в найденном",
                 "unverified": "Не подтверждено", "clarify": "Уточнение", "info": "Справка", "error": "Ошибка"}

# метка над ответом: (текст, цвет, значок) — статус передаётся и словом, и значком, не только цветом
BADGES = {
    "documents": ("Ответ по документам", "violet", ":material/verified:"),
    "database": ("Ответ по учётной базе", "blue", ":material/table_chart:"),
    "both": ("Учётная база и документы", "violet", ":material/join:"),
    "normo": ("Нормоконтроль по ГОСТ Р 7.0.97", "green", ":material/fact_check:"),
    "no_data": ("Ответа в источниках нет", "gray", ":material/search_off:"),
    "model_refused": ("Ответа в найденном нет", "gray", ":material/search_off:"),
    "unverified": ("Ответ не подтверждён ссылкой", "orange", ":material/warning:"),
    "clarify": ("Нужно уточнение", "orange", ":material/help:"),
    "info": ("Справка", "gray", ":material/info:"),
    "error": ("Ошибка", "red", ":material/error:"),
}

CSS = """
<style>
/* загрузка файлов — по-русски (у компонента Streamlit нет настройки языка) */
[data-testid="stFileUploaderDropzoneInstructions"] span { display: none; }
[data-testid="stFileUploaderDropzoneInstructions"] small { display: none; }
[data-testid="stFileUploaderDropzoneInstructions"] > div::before {
    content: "Перетащите PDF или DOCX сюда"; font-size: .875rem; }
[data-testid="stFileUploaderDropzoneInstructions"] > div::after {
    content: "до 100 МБ на файл"; display: block; font-size: .75rem; opacity: .7; }
[data-testid="stFileUploaderDropzone"] button [data-testid="stMarkdownContainer"] p { font-size: 0 !important; }
[data-testid="stFileUploaderDropzone"] button [data-testid="stMarkdownContainer"] p::after {
    content: "Выбрать файлы"; font-size: .875rem !important; }
/* кнопки-источники под ответом — компактнее */
.st-key-sources button { padding: .15rem .6rem; min-height: 0; text-align: left; justify-content: flex-start; }
.st-key-sources button p { font-size: .82rem; }
/* боковая панель — плотнее: на экране ноутбука 1366×768 видны и диалоги, и пользователь с «Выйти» */
[data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] { gap: .55rem; }
[data-testid="stSidebarUserContent"] hr { margin: .4rem 0; }
/* подписи — контрастнее на светлом фоне */
[data-testid="stCaptionContainer"] { color: #5C5470; }
</style>
"""


def inject_css(extra: str = "") -> None:
    st.markdown(CSS + extra, unsafe_allow_html=True)


@st.cache_resource(show_spinner="Загружаю базу знаний…")
def get_agent() -> Agent:
    return Agent()


@st.cache_data(show_spinner=False, max_entries=300)
def page_png(path: str, page: int, fragment: str, crop: bool = False) -> tuple[bytes, int]:
    return render_page(path, page, fragment, zoom=2.0 if crop else 1.4, crop=crop)


@st.cache_data(show_spinner=False, max_entries=50)
def file_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


@st.cache_data(ttl=15, show_spinner=False)
def health() -> dict[str, bool]:
    return llm.health()


def index_meta() -> dict:
    path = settings.index_dir / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ctx() -> dict:
    """Текущий пользователь: login, name, role, is_admin (заполняет точка входа)."""
    return st.session_state["ctx"]

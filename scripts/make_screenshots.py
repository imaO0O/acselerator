"""Скриншоты ключевых экранов для презентации — 1920×1080, в docs/screens/.

    pip install playwright          (один раз; браузер не скачивается — используется Microsoft Edge)
    scripts\\start_servers.bat  и  scripts\\run_chat.bat
    python scripts/make_screenshots.py

Входит администратором, в режиме показа (без служебных блоков) задаёт вопросы сценария и снимает
экраны: пустой экран с примерами, ответ со ссылками, окно источника с выделенным фрагментом,
«запись + правило», ответ по учёту, честный отказ, встречный вопрос, вопрос об агенте, страницы
«Пробелы в документации», «Качество» и «Безопасность». Перед запуском —
python scripts/check_demo.py --clean (чистый список диалогов).
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_users import DEMO_USERS  # noqa: E402

URL = "http://localhost:8501"
OUT = ROOT / "docs" / "screens"
SHOTS = [   # (файл, вопрос, нажать первую кнопку-источник)
    ("02_otvet_so_ssylkami.png", "Что делать при искрении оборудования?", False),
    ("03_istochnik.png", None, True),
    ("04_zapis_i_pravilo.png", "Можно ли выдать в производство партию П-2609-04?", False),
    ("05_uchet.png", "У каких средств измерений истёк срок поверки?", False),
    ("06_chestnyy_otkaz.png", "Сколько стоит поверка одного осциллографа?", False),
    ("07_utochnenie.png", "Какой срок?", False),
    ("08_bez_interneta.png", "Ты работаешь без интернета?", False),
]
ADMIN_TABS = [("09_probely.png", "Пробелы в документации"), ("10_kachestvo.png", "Качество"),
              ("11_bezopasnost.png", "Безопасность")]


def wait_answer(page) -> None:
    """Ждём, пока ответ дорисуется: в последнем сообщении — подпись источника («Документы · 1,2 с»,
    «Справка»), на странице нет устаревших элементов перерисовки и статуса «Ищу ответ…»."""
    page.wait_for_function(
        """() => {
            const msgs = document.querySelectorAll('[data-testid=stChatMessage]');
            const last = msgs[msgs.length - 1];
            return last && /Документы|Учётная база|База и документы|Справка/.test(last.innerText)
                && !document.querySelector('[data-stale=true]') && !document.body.innerText.includes('Ищу ответ');
        }""", timeout=90000)
    time.sleep(1.5)


def new_dialog(page) -> None:
    page.get_by_text("Новый диалог").first.click()
    page.wait_for_selector("text=Например", timeout=30000)
    time.sleep(1)


def main() -> None:
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    login, _, _, password = next(u for u in DEMO_USERS if u[0] == "admin")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(URL)
        page.get_by_label("Логин").fill(login)
        page.get_by_label("Пароль").fill(password)
        page.get_by_role("button", name="Войти").click()
        page.wait_for_selector("text=Новый диалог", timeout=30000)
        page.goto(f"{URL}/?show=1")
        page.wait_for_selector("text=Новый диалог", timeout=30000)
        new_dialog(page)
        page.screenshot(path=OUT / "01_start.png")
        print("01_start.png")

        for name, question, open_source in SHOTS:
            if question:
                new_dialog(page)          # на каждом кадре — один вопрос и ответ на чистом экране
                box = page.get_by_placeholder("Спросите о регламентах")
                box.fill(question)
                box.press("Enter")
                wait_answer(page)
                page.mouse.wheel(0, -2000)
                time.sleep(0.5)
            if open_source:
                page.locator("[class*=st-key-sources] button").first.click()
                page.wait_for_selector("[role=dialog] img", timeout=30000)
                time.sleep(1.5)
            page.screenshot(path=OUT / name)
            print(name)
            if open_source:
                page.keyboard.press("Escape")
                time.sleep(1)

        page.goto(f"{URL}/admin")
        page.wait_for_selector("text=Администрирование", timeout=30000)
        for name, tab in ADMIN_TABS:
            page.get_by_role("tab", name=tab).click()
            time.sleep(2)
            page.screenshot(path=OUT / name)
            print(name)
        browser.close()
    print(f"Готово: {OUT}")


if __name__ == "__main__":
    main()

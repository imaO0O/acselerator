"""Скриншоты ключевых экранов для презентации — 1920×1080, в docs/screens/.

    pip install playwright          (один раз; браузер не скачивается — используется Microsoft Edge)
    scripts\\start_servers.bat  и  scripts\\run_chat.bat
    python scripts/make_screenshots.py
    python scripts/make_screenshots.py --admin      (только страницы администрирования, кадры 09–11)
    python scripts/make_screenshots.py --client     (только запросы клиентов о приборах, кадры 14–24 и 26)

Входит администратором, в режиме показа (без служебных блоков) задаёт вопросы сценария и снимает
экраны: пустой экран с примерами, ответ со ссылками, окно источника с выделенным фрагментом,
«запись + правило», ответ по учёту, честный отказ, встречный вопрос, вопрос об агенте, сравнение
редакций, нормоконтроль приложенного приказа, страницы
«Пробелы в документации», «Качество» и «Безопасность». С --client входит специалистом техпродаж и снимает
запросы клиентов о приборах: письмо клиента → черновик ТКП и его утверждение, подбор со сравнением
с обычным поиском, черновик ответа, противоречие источников с решением эксперта и ответ уже с подтверждённым
значением, сравнение моделей; затем администратором — «Эффект», «Вопросы эксперту», реестр и справочник. Перед запуском —
python scripts/check_demo.py --clean (чистый список диалогов).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
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
    ("12_redakcii.png", "Что изменилось в СТП ДП-02?", False),
]
NORMO = ("13_normokontrol.png", ROOT / "data" / "show" / "Prikaz_s_oshibkami.docx")
ADMIN_TABS = [("09_probely.png", "Пробелы в документации"), ("10_kachestvo.png", "Качество"),
              ("11_bezopasnost.png", "Безопасность")]
PODBOR = "Клиенту нужен осциллограф с полосой не менее 150 МГц и 4 каналами — что предложить?"
MAS = "Можно ли мультиметром MAS830L измерить ток 15 А?"
DISKR = "Какая частота дискретизации у АКИП-4115/1В?"
SRAVN = "Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)?"
CLIENT_ADMIN_TABS = [("21_effekt.png", "Эффект"), ("22_eksperty.png", "Вопросы эксперту"),
                     ("23_reestr.png", "Документы"), ("24_spravochnik.png", "Справочник")]


def wait_answer(page) -> None:
    """Ждём, пока ответ дорисуется: в последнем сообщении — подпись источника («Документы · 1,2 с»,
    «Справка»), на странице нет устаревших элементов перерисовки и статуса «Ищу ответ…»."""
    page.wait_for_function(
        """() => {
            const msgs = document.querySelectorAll('[data-testid=stChatMessage]');
            const last = msgs[msgs.length - 1];
            return last && /Документы|Учётная база|База и документы|Справочник|Запрос клиента|Расчёт|Справка|Нормоконтроль/
                .test(last.innerText)
                && !document.querySelector('[data-stale=true]') && !document.body.innerText.includes('Ищу ответ');
        }""", timeout=90000)
    time.sleep(1.5)


def to_question(page) -> None:
    """Кадр — с начала: вопрос сотрудника вверху экрана, под ним ответ."""
    page.evaluate("""() => { const m = document.querySelectorAll('[data-testid=stChatMessage]');
                             if (m.length >= 2) m[m.length - 2].scrollIntoView({block: 'start'}); }""")
    time.sleep(0.8)


def new_dialog(page) -> None:
    page.get_by_text("Новый диалог").first.click()
    page.wait_for_selector("text=Например", timeout=30000)
    time.sleep(1)


def chat(page) -> None:
    """Кадры чата 01–08, 12, 13: вопросы сценария в режиме показа."""
    page.goto(f"{URL}/?show=1")
    page.wait_for_selector("text=Новый диалог", timeout=30000)
    new_dialog(page)
    page.screenshot(path=OUT / "01_start.png")
    print("01_start.png")

    for name, question, open_source in SHOTS:
        if question:
            new_dialog(page)          # на каждом кадре — один вопрос и ответ на чистом экране
            box = page.get_by_placeholder("Спросите о")
            box.fill(question)
            box.press("Enter")
            wait_answer(page)
            to_question(page)
        if open_source:
            page.locator("[class*=st-key-sources] button").first.click()
            page.wait_for_selector("[role=dialog] img", timeout=30000)
            time.sleep(1.5)
        page.screenshot(path=OUT / name)
        print(name)
        if open_source:
            page.keyboard.press("Escape")
            time.sleep(1)

    name, path = NORMO                    # нормоконтроль: документ Word приложен к сообщению
    if path.exists():
        new_dialog(page)
        page.locator("[data-testid=stChatInput] input[type=file]").set_input_files(str(path))
        time.sleep(1)
        box = page.get_by_placeholder("Спросите о")
        box.fill("Проверь оформление")
        box.press("Enter")
        wait_answer(page)
        to_question(page)
        page.screenshot(path=OUT / name)
        print(name)


def ask(page, question: str) -> None:
    new_dialog(page)
    box = page.get_by_placeholder("Спросите о")
    box.fill(question)
    box.press("Enter")
    wait_answer(page)
    to_question(page)


def shot(page, name: str) -> None:
    page.mouse.move(1900, 700)                 # без всплывающих подсказок у курсора и у кнопки в фокусе
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    time.sleep(2)
    page.screenshot(path=OUT / name)
    print(name)


def last_message(page):
    return page.locator("[data-testid=stChatMessage]").last


def approve(page, edit: str = "") -> None:
    """В окне черновика: правка текста сотрудником и «Утвердить и скачать» (файл не сохраняется)."""
    if edit:
        box = page.locator("[role=dialog] textarea")
        box.fill(box.input_value().replace("Добрый день!", edit, 1))
        time.sleep(1)
    with page.expect_download():
        page.locator("[role=dialog]").get_by_role("button", name="Утвердить").click()
    time.sleep(1.5)
    page.keyboard.press("Escape")
    time.sleep(1.5)


def client(page) -> None:
    """Кадры 14–20: главный сценарий показа под ролью «Техпродажи и сервис»."""
    from kbagent import experts
    page.goto(f"{URL}/?show=1")
    page.wait_for_selector("text=Новый диалог", timeout=30000)

    new_dialog(page)                                   # письмо клиента → черновик ТКП
    page.get_by_role("button", name="Пример: письмо клиента").click()
    wait_answer(page)
    to_question(page)
    shot(page, "14_tkp.png")
    last_message(page).get_by_role("button", name="Черновик ТКП").click()
    page.wait_for_selector("[role=dialog] textarea", timeout=30000)
    time.sleep(1.5)
    shot(page, "15_tkp_chernovik.png")
    approve(page, "Добрый день, Иван Петрович!")

    ask(page, PODBOR)                                  # подбор и что выдал бы обычный поиск
    last_message(page).get_by_text("Обычный поиск выдал бы").click()
    time.sleep(1.5)
    shot(page, "16_podbor.png")

    ask(page, MAS)                                     # черновик ответа, утверждение, вопрос эксперту
    last_message(page).get_by_role("button", name="Черновик").click()
    page.wait_for_selector("[role=dialog] textarea", timeout=30000)
    time.sleep(1.5)
    shot(page, "17_chernovik.png")
    approve(page)
    last_message(page).get_by_role("button", name="Эксперту").click()
    page.wait_for_selector("[role=dialog] textarea", timeout=30000)
    page.locator("[role=dialog] textarea").fill("клиент спрашивает, есть ли модификация MAS830L на 20 А")
    page.locator("[role=dialog]").get_by_role("button", name="Передать").click()
    page.wait_for_selector("text=решение появится здесь", timeout=30000)
    time.sleep(1)

    ask(page, DISKR)                                   # противоречие → эксперт выбрал верное значение
    for it in experts.load():
        if it["status"] == "открыт" and it["subject"].startswith("АКИП-4115/1В"):
            right = next(d for d in it["details"] if d["value"].startswith("1 Гвыб"))
            experts.resolve(it["id"], "эксперт технического отдела", "на с. 6 опечатка в руководстве", right)
    page.mouse.click(1800, 500)                        # фокус — не в поле ввода; R — перезапуск страницы Streamlit
    page.keyboard.press("r")
    page.wait_for_selector("text=Ответ эксперта", timeout=30000)
    time.sleep(1.5)
    to_question(page)
    shot(page, "18_protivorechie.png")

    ask(page, DISKR)                                   # тот же вопрос позже — уже подтверждённое значение
    shot(page, "19_podtverzhdeno.png")

    ask(page, SRAVN)
    shot(page, "20_sravnenie.png")

    letter = ROOT / "data" / "show" / "Pismo_klienta.docx"      # письмо клиента — файлом в скрепке
    if letter.exists():
        new_dialog(page)
        page.locator("[data-testid=stChatInput] input[type=file]").set_input_files(str(letter))
        time.sleep(1)
        box = page.get_by_placeholder("Спросите о")
        box.fill("Подготовь ТКП")
        box.press("Enter")
        wait_answer(page)
        to_question(page)
        shot(page, "26_pismo_fajlom.png")


def login(browser, who: str):
    user, _, _, password = next(u for u in DEMO_USERS if u[0] == who)
    page = browser.new_context(viewport={"width": 1920, "height": 1080}).new_page()
    page.goto(URL)
    page.get_by_label("Логин").fill(user)
    page.get_by_label("Пароль").fill(password)
    page.get_by_role("button", name="Войти").click()
    page.wait_for_selector("text=Новый диалог", timeout=30000)
    return page


def main() -> None:
    from playwright.sync_api import sync_playwright
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--admin", action="store_true", help="только страницы администрирования (кадры 09–11)")
    ap.add_argument("--client", action="store_true", help="только запросы клиентов о приборах (кадры 14–20)")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        if args.client:
            client(login(browser, "prodazhi"))
        page = login(browser, "admin")
        if not args.admin and not args.client:
            chat(page)
        page.goto(f"{URL}/admin")
        page.wait_for_selector("text=Администрирование", timeout=30000)
        for name, tab in (CLIENT_ADMIN_TABS if args.client else ADMIN_TABS):
            page.get_by_role("tab", name=tab).click()
            time.sleep(2)
            closed = page.get_by_text("Закрытые пробелы")
            if tab == "Пробелы в документации" and closed.count():
                closed.first.click()          # видно, что пробел закрылся после загрузки документа
                time.sleep(1.5)
            page.screenshot(path=OUT / name)
            print(name)
        browser.close()
    print(f"Готово: {OUT}")


if __name__ == "__main__":
    main()

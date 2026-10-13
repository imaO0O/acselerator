"""Проверка перед показом или записью скринкаста — одной командой.

    python scripts/check_demo.py            проверить модели, базу знаний и весь сценарий показа
    python scripts/check_demo.py --clean    и очистить диалоги демо-учёток (чистый список слева)
    python scripts/check_demo.py --reset-show   убрать загруженные документы для показа, чтобы сцену
                                                «загрузили документ — агент ответил» можно было повторить

Проверяет: серверы моделей, индекс и учётную базу; задаёт вопросы сценария (docs/demo_script.md) под
нужными ролями и сверяет ответы; заодно прогревает модели — первый ответ после запуска дольше.
В конце — «ГОТОВО К ПОКАЗУ» или список того, что не так. Запросы пишутся в журнал от служебного
пользователя «eval» и на странице «Журнал запросов» по умолчанию скрыты.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import auth, llm  # noqa: E402
from kbagent.config import settings  # noqa: E402

ADMIN, ENGINEER, METROLOGIST, SALES = "Администратор", "Инженер производства", "Метролог", "Техпродажи и сервис"
SHOW_DOCS = ("RI-DP-05_selektivnaya_pajka", "Prikaz_17_skan")
TKP_EXAMPLE = ("Подготовь ТКП по письму клиента: «Просим предложить 2 осциллографа с полосой не менее 150 МГц "
               "и 4 каналами, мультиметр для измерения тока до 15 А — 3 шт., паяльную станцию для пайки при "
               "450 °C и источник питания 30 В»")       # тот же текст — кнопка-пример в чате (app/ui_chat.py)
SLOW = 5.0     # секунд: дольше — предупреждение

# (роль, вопрос, предыдущий вопрос, ожидаемый итог, что должно быть в ответе)
# итог: answer — ответ; refuse — честный отказ; clarify — встречный вопрос; info — справка
SCENARIO = [
    (ADMIN, "Что делать при искрении оборудования?", None, "answer", ["2211"]),
    (ADMIN, "А если это случилось ночью?", "Что делать при искрении оборудования?", "answer", ["2215"]),
    (ADMIN, "Можно ли выдать в производство партию П-2609-04?", None, "answer", ["карантин"]),
    (ADMIN, "Где лежит флюс?", None, "answer", ["Шкаф ЛВЖ"]),
    (ADMIN, "У каких средств измерений истёк срок поверки?", None, "answer", ["СИ-0012"]),
    (ADMIN, "Какие документы есть про паяльные станции?", None, "answer", ["Паяльная станция"]),
    (ADMIN, "Сколько стоит поверка одного осциллографа?", None, "refuse", []),
    (ADMIN, "По какому телефону сообщить диспетчеру об отказе оборудования?", None, "answer", ["2211"]),
    (ADMIN, "Какой срок?", None, "clarify", []),
    (ADMIN, "Ты работаешь без интернета?", None, "info", ["интернет"]),
    (ADMIN, "Что изменилось в СТП ДП-02?", None, "answer", ["Изменено пунктов", "2211"]),
    # сценарий по карточке партнёра: запросы клиентов о приборах — технические продажи и сервис
    (SALES, TKP_EXAMPLE, None, "answer", ["Разбор запроса клиента", "АКИП-4143/1", "DT9205A", "Источник питания", "3 шт"]),
    (SALES, "Клиенту нужен осциллограф с полосой не менее 150 МГц и 4 каналами — что предложить?", None,
     "answer", ["АКИП-4143", "Подходят: 6"]),
    (SALES, "Можно ли мультиметром MAS830L измерить ток 15 А?", None, "answer", ["не подходит", "10 А"]),
    (SALES, "Какая частота дискретизации у АКИП-4115/1В?", None, "answer", ["Источники расходятся"]),
    (SALES, "Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)?", None, "answer", ["Различаются"]),
    (SALES, "Подойдёт ли паяльная станция SBK936B для пайки при 450 °C?", None, "answer", ["подходит", "200–480"]),
    (SALES, "У каких средств измерений истёк срок поверки?", None, "answer", ["СИ-0012"]),
    (ENGINEER, "Пришла партия из 250 резисторов. Сколько штук осмотреть внешне и сколько проверить по "
               "электрическим параметрам?", None, "answer", ["25 шт", "13 шт"]),
    (ENGINEER, "На какой срок выдаётся наряд-допуск?", None, "answer", ["15 календарных дней"]),
    (ENGINEER, "За сколько дней до истечения срока поверки метролог уведомляет подразделение?", None, "refuse", []),
    (METROLOGIST, "За сколько дней до истечения срока поверки метролог уведомляет подразделение?", None,
     "answer", ["30"]),
]
# до загрузки документа для показа агент отвечать не должен — иначе сцена «до и после» не получится
SHOW_QUESTION = "Какая температура припоя в ванне селективной пайки для ПОС-61?"


def ok(result, expect: str, must: list[str]) -> bool:
    if expect == "answer" and result.status != "answer":
        return False
    if expect == "refuse" and result.answered:
        return False
    if expect in ("clarify", "info") and result.status != expect:
        return False
    text = result.text.lower().replace("ё", "е")
    return all(m.lower().replace("ё", "е") in text for m in must)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clean", action="store_true", help="очистить диалоги демо-учёток и очередь вопросов эксперту")
    ap.add_argument("--reset-show", action="store_true", help="убрать загруженные документы для показа")
    args = ap.parse_args()
    problems = []
    if args.clean:
        # показ — с чистого листа: очередь эксперта и вкладка «Эффект» наполняются уже на показе
        from kbagent import effect, experts
        experts._save([])
        effect._path().unlink(missing_ok=True)

    print("1. Модели")
    state = llm.health()
    for name, up in state.items():
        print(f"   {'✓' if up else '✗'} {name}")
    if not all(state.values()):
        print("\nСерверы моделей не отвечают. Запустите scripts\\start_servers.bat и повторите проверку.")
        return 1

    print("2. База знаний и учётная база")
    from kbagent.agent import Agent  # загрузка индекса — после проверки моделей
    from kbagent import uploads
    agent = Agent()
    docs = {c.doc_id for c in agent.index.chunks}
    print(f"   ✓ документов: {len(docs)}, фрагментов: {len(agent.index.chunks)}")
    from kbagent import demo_db
    if settings.db_path == demo_db.DB:
        demo_db.ensure_current()          # демо-база создаётся и обновляет даты от сегодняшнего дня
    if not settings.db_path.exists():
        problems.append(f"нет учётной базы {settings.db_path}")

    if args.reset_show:
        for d in uploads.list_uploads():
            if any(d["id"] == uploads.doc_id_for(name) for name in SHOW_DOCS):
                uploads.remove_upload(agent.index, d["id"])
                print(f"   ✓ убран загруженный для показа «{d['title']}»")

    print("3. Сценарий показа")
    times = []
    for role, question, prev, expect, must in SCENARIO:
        history = None
        if prev:
            first = agent.ask(prev, role=role, user="eval")
            history = [{"question": prev, "text": first.text,
                        "docs": [s["doc_id"] for s in first.sources if s["cited"]]}]
        t = time.time()
        r = agent.ask(question, role=role, history=history, user="eval")
        took = time.time() - t
        times.append(took)
        good = ok(r, expect, must)
        mark = "✓" if good else "✗"
        slow = f"  (медленно: {took:.1f} с)" if took > SLOW else ""
        print(f"   {mark} {took:4.1f} с  [{role}] {question}{slow}")
        if not good:
            problems.append(f"«{question}» ({role}): ожидалось {expect} {must}, получено {r.status}: {r.text[:120]}")

    print("4. Сцены с загрузкой документа и нормоконтролем")
    uploaded = {d["id"] for d in uploads.list_uploads()}
    if uploads.doc_id_for(SHOW_DOCS[0]) in uploaded:
        problems.append("документ для показа загрузки уже загружен — запустите с --reset-show")
        print("   ✗ RI-DP-05 уже загружен")
    else:
        r = agent.ask(SHOW_QUESTION, role=ADMIN, user="eval")
        print(f"   {'✓' if not r.answered else '✗'} до загрузки агент не знает ответа: «{SHOW_QUESTION}»")
        if r.answered:
            problems.append(f"на «{SHOW_QUESTION}» агент отвечает ещё до загрузки документа")
    for name in SHOW_DOCS:
        if not (settings.raw_dir.parent / "show" / f"{name}.pdf").exists():
            problems.append(f"нет data/show/{name}.pdf — запустите python scripts/make_show_docs.py")
    order = settings.raw_dir.parent / "show" / "Prikaz_s_oshibkami.docx"
    if order.exists():
        r = agent.normocontrol(order.name, order.read_bytes(), user="eval")
        good = r.status == "answer" and "нарушений — 6" in r.text
        print(f"   {'✓' if good else '✗'} нормоконтроль приказа с ошибками: {r.text.splitlines()[0]}")
        if not good:
            problems.append(f"нормоконтроль приказа с ошибками: {r.text[:120]}")
    else:
        problems.append("нет data/show/Prikaz_s_oshibkami.docx — запустите python scripts/make_show_docs.py")

    if args.clean:
        print("5. Диалоги демо-учёток")
        for login in ("admin", "inzhener", "metrolog", "prodazhi"):
            convs = auth.list_conversations(login)
            for c in convs:
                auth.delete_conversation(login, c["id"])
            print(f"   ✓ {login}: удалено диалогов — {len(convs)}")
        # очередь эксперта — с чистого листа: на показе её наполняют сцена П3 и кнопка «Эксперту»
        from kbagent import experts
        removed = len(experts.load())
        experts._save([])
        print(f"   ✓ вопросы эксперту и вкладка «Эффект» очищены (вопросов эксперту было — {removed})")

    times.sort()
    print(f"\nВремя ответа: медиана {times[len(times) // 2]:.1f} с, максимум {times[-1]:.1f} с")
    if problems:
        print("\nНЕ ГОТОВО:")
        for p in problems:
            print(f" - {p}")
        return 1
    print("\nГОТОВО К ПОКАЗУ")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Загрузка документов: новая редакция до утверждения и совпадение длинных имён файлов.
После проверки всё загруженное удаляется."""
import io
import sys
from pathlib import Path

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # корень репозитория
from kbagent import uploads  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.ingest import load_catalog  # noqa: E402
from kbagent.config import settings  # noqa: E402

ENG = "Инженер производства"


def docx_bytes(lines):
    d = Document()
    for ln in lines:
        d.add_paragraph(ln)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


agent = Agent()
cat = load_catalog(settings.raw_dir)
dp02 = next(k for k, v in cat.items() if "ДП-02" in str(v.get("title", "")) and "2024" not in str(v.get("title", ""))
            and "2024" not in k)
print("действующая редакция СТП ДП-02:", dp02, "—", cat[dp02].get("title"))
Q = "По какому телефону сообщить диспетчеру об отказе категории А?"


def ask(tag):
    r = agent.ask(Q, role=ENG, user="tester_u2")
    print(f"  [{tag}] {r.status}: {r.text[:260]!r}\n      источники: {[s['label'][:80] for s in r.sources if s['cited']]}")


ask("до загрузки")
# 1. Новая редакция, загруженная по ошибке (другой телефон) и ещё НЕ утверждённая
fake = docx_bytes(["СТП ДП-02-2027", "ДЕЙСТВИЯ ПЕРСОНАЛА ПРИ ОТКАЗЕ ТЕХНОЛОГИЧЕСКОГО ОБОРУДОВАНИЯ",
                   "3 Первоочередные действия при отказе",
                   "3.1 При отказе категории А работник обязан немедленно нажать кнопку аварийной остановки, "
                   "отключить оборудование и сообщить мастеру участка и дежурному диспетчеру по внутреннему телефону 9999."])
info = uploads.add_upload(agent.index, "STP_DP-02_proekt_novoj_redakcii.docx", fake, access="all",
                          user="tester_u2", replaces=dp02)
agent = Agent()          # как после перезапуска чата — каталог и индекс перечитаны
print("загружено (не утверждено):", info["id"])
cat = load_catalog(settings.raw_dir)
print("  статус старой редакции в каталоге:", cat[dp02].get("status", "действует"))
ask("после загрузки, до утверждения")
uploads.remove_upload(agent.index, info["id"], user="tester_u2")
agent = Agent()
ask("после удаления загрузки")

# 2. Два разных документа с длинными похожими именами
a = "Инструкция по охране труда при работе на паяльной станции участка поверхностного монтажа А.docx"
b = "Инструкция по охране труда при работе на паяльной станции участка поверхностного монтажа Б.docx"
ia = uploads.add_upload(agent.index, a, docx_bytes(["Инструкция А", "1.1 Температура жала не выше 350 °C."]),
                        user="tester_u2")
ib = uploads.add_upload(agent.index, b, docx_bytes(["Инструкция Б", "1.1 Температура жала не выше 420 °C."]),
                        user="tester_u2")
print(f"\nid А: {ia['id']}\nid Б: {ib['id']}\nвторой «заменил одноимённый»: {ib['replaced_same']}")
ups = [u for u in uploads.list_uploads() if u.get("uploaded_by") == "tester_u2"]
print("в каталоге загружено тестером:", [(u["id"][:40], u["title"][:50]) for u in ups])
for u in ups:
    uploads.remove_upload(agent.index, u["id"], user="tester_u2")
print("тестовые загрузки удалены:", not [u for u in uploads.list_uploads() if u.get("uploaded_by") == "tester_u2"])

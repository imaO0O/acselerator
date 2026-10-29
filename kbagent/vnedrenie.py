"""Внедрение у заказчика: чек-лист с проверкой каждого шага по состоянию системы и подключения без правки файлов.

Шаги внедрения (7–11 дней в финмодели) — это в основном «завести, загрузить, подключить, проверить». Чек-лист
сам видит, что уже сделано: демо-учётки убраны, документы предприятия подключены, справочник и прайс загружены,
бланк ТКП заполнен, задачи по расписанию зарегистрированы, есть резервная копия, прогон на вопросах предприятия;
и говорит, где сделать остальное. Подключения (папка отдела, почта, 1С) пишутся в data/settings.env — те же
настройки видят чат, REST API и задачи по расписанию; пароли — в отдельные файлы, в форму они не возвращаются.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, SETTINGS_FILE, settings

CUSTOMER_DOCS = ("uploaded", "folder", "partner")      # папки data/raw с документами заказчика (не демо)
DOC_EXT = (".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
# переменная → атрибут settings и значение по умолчанию: новое значение подхватывается без перезапуска чата
# (настройки 1С kbagent/onec.py читает из окружения при каждой загрузке)
FIELDS = {"KB_DOCS_FOLDER": ("docs_folder", ""), "KB_DOCS_FOLDER_ACCESS": ("docs_folder_access", "all"),
          "KB_MAIL_HOST": ("mail_host", ""), "KB_MAIL_PORT": ("mail_port", 993), "KB_MAIL_USER": ("mail_user", ""),
          "KB_MAIL_FOLDER": ("mail_folder", "INBOX")}
HEADER = ("# Настройки установки (KB_…=значение): их видят чат, REST API и задачи по расписанию. "
          "Правятся на странице «Администрирование» → «Внедрение» или здесь; образец — settings.env.example.")


def current(key: str) -> str:
    return os.environ.get(key, "").strip()


def write_settings(values: dict[str, str], path: Path = SETTINGS_FILE) -> None:
    """Меняет только переданные строки KB_…; комментарии и прочие настройки файла остаются. Пустое — убрать."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else [HEADER]
    left, seen, out = dict(values), set(), []
    for line in lines:
        key = line.lstrip("# ").partition("=")[0].strip()
        if key in left:
            value = left.pop(key).strip()
            seen.add(key)
            out.append(f"{key}={value}" if value else f"# {key}=")
        elif key in seen and not line.lstrip().startswith("#"):
            continue                                    # повтор той же настройки ниже — уже заменён
        else:
            out.append(line)
    out += [f"{k}={v.strip()}" for k, v in left.items() if v.strip()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


def apply(values: dict[str, str]) -> None:
    """Новые значения — в работающий процесс: чат видит их сразу, без перезапуска."""
    for key, value in values.items():
        value = value.strip()
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
        if key in FIELDS:
            attr, default = FIELDS[key]
            setattr(settings, attr, type(default)(value) if value else default)


def save(values: dict[str, str], path: Path = SETTINGS_FILE) -> None:
    write_settings(values, path)
    apply(values)


def save_password(kind: str, password: str) -> None:
    """Пароль почты или 1С — в свой файл в data/ (в git и резервную копию не входит)."""
    from . import mailbox, onec
    target = {"mail": mailbox.PASSWORD_FILE, "1c": onec.PASSWORD_FILE}[kind]
    target.write_text(password.strip(), encoding="utf-8")


def has_password(kind: str) -> bool:
    from . import mailbox, onec
    env, target = {"mail": ("KB_MAIL_PASSWORD", mailbox.PASSWORD_FILE),
                   "1c": ("KB_1C_PASSWORD", onec.PASSWORD_FILE)}[kind]
    return bool(os.environ.get(env) or (target.exists() and target.read_text(encoding="utf-8").strip()))


# ---------- проверка подключений ----------

def check_folder(path: str) -> tuple[bool, str]:
    from .folder_sync import _scan
    if not path:
        return False, "папка не задана"
    folder = Path(path)
    if not folder.is_dir():
        return False, f"папка «{path}» недоступна с этого компьютера — проверьте путь и права учётной записи"
    try:
        n = len(_scan(folder))
    except OSError as e:
        return False, f"папку не удалось прочитать: {e}"
    return True, f"папка доступна, документов (PDF, DOCX, Excel, сканы): {n}"


def check_backup_copy(path: str) -> tuple[bool, str]:
    """Папка для копии архивов: доступна ли на запись и не на том ли она диске, что система."""
    from .backup import same_disk
    if not path:
        return False, "папка не задана"
    folder = Path(path)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".kbagent_write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        return False, f"в папку «{path}» нельзя записать: {e}"
    if same_disk(folder):
        return False, "папка на том же диске, что и система — при отказе диска пропадут и архивы, и копии"
    return True, "папка доступна на запись, диск — другой"


def check_mail() -> tuple[bool, str]:
    import imaplib
    from . import mailbox
    if not mailbox.configured():
        return False, "не заданы сервер и ящик"
    try:
        conn = mailbox._connect()
    except (OSError, imaplib.IMAP4.error) as e:
        return False, f"не удалось войти в ящик: {e}"
    try:
        typ, data = conn.select(settings.mail_folder, readonly=True)
        if typ != "OK":
            return False, f"вход выполнен, но папки «{settings.mail_folder}» нет"
        return True, f"вход выполнен, в папке «{settings.mail_folder}» писем: {int(data[0] or 0)} (только чтение)"
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def check_1c(session=None) -> tuple[bool, str]:
    from . import onec
    if not onec.configured():
        return False, "не заданы адрес, пользователь и тип цен"
    c = onec.conf()
    try:
        types = onec._get(c, c["price_types"], {"$select": "Ref_Key,Description"}, session)
    except onec.OneCError as e:
        return False, str(e)
    names = [t.get("Description", "").strip() for t in types]
    if c["price_type"].lower() not in (n.lower() for n in names):
        return False, f"1С отвечает, но типа цен «{c['price_type']}» нет. Есть: " + ", ".join(names[:15])
    return True, f"1С отвечает ({c['title']}), тип цен «{c['price_type']}» найден"


# ---------- чек-лист ----------

@dataclass
class Step:
    key: str
    title: str
    done: bool | None              # None — шаг не нужен этому заказчику (почта, папка, 1С не подключаются)
    detail: str
    how: str                       # где сделать
    optional: bool = False


def customer_documents() -> int:
    return sum(1 for d in CUSTOMER_DOCS if (settings.raw_dir / d).is_dir()
               for p in (settings.raw_dir / d).rglob("*") if p.is_file() and p.suffix.lower() in DOC_EXT)


def _done(actions: list[dict], *names: str) -> dict | None:
    return next((a for a in reversed(actions) if a["action"] in names), None)


def steps(models_ok: bool | None = None, registered: dict[str, bool] | None = None) -> list[Step]:
    from . import jobs, onec, prajs, spravochnik, tkp_registry, users
    from .actions import load_actions
    actions = load_actions()
    out = []
    if models_ok is not None:
        out.append(Step("models", "Модели запущены", models_ok,
                        "GigaChat, поиск по смыслу и переранжировщик отвечают" if models_ok else "модели не отвечают",
                        "scripts\\start_servers.bat (-SmallGpu на ноутбуке, -Cpu без видеокарты)"))
    demo = users.demo_accounts()
    out.append(Step("demo", "Демо-учётки убраны", not demo,
                    "демо-паролей нет" if not demo else "активны с опубликованными паролями: " + ", ".join(demo),
                    "scripts\\first_run.py или «Пользователи» → сбросить пароль / удалить"))
    people = [u for u in users.listing() if u["active"] and u["login"] not in users.DEMO_PASSWORDS]
    out.append(Step("users", "Сотрудники заведены", len(people) >= 2,
                    f"активных учётных записей заказчика: {len(people)}",
                    "«Пользователи» → «Новый пользователь»: роль определяет, какие документы видны"))
    n = customer_documents()
    out.append(Step("documents", "Документы предприятия подключены", n > 0,
                    f"документов заказчика: {n}" if n else "в базе только демонстрационные документы",
                    "«Документы» → загрузить, или папка отдела ниже"))
    if settings.docs_folder:
        from .folder_sync import last_report
        rep = last_report() or {}
        ok = bool(rep) and not rep.get("errors") and not rep.get("warning")
        out.append(Step("folder", "Папка документов отдела", ok,
                        f"последняя проверка {rep['started']}: ошибок {len(rep.get('errors', []))}"
                        + (f"; {rep['warning']}" if rep.get("warning") else "") if rep else "ещё не проверялась",
                        "«Документы» → «Проверить папку сейчас»; дальше — по расписанию", optional=True))
    else:
        out.append(Step("folder", "Папка документов отдела", None, "не подключена — если документы в сетевой "
                        "папке, подключите её ниже", "«Подключения» ниже", optional=True))
    values, _ = spravochnik.load()
    loaded = _done(actions, "spravochnik")
    out.append(Step("spravochnik", "Справочник параметров заказчика", bool(loaded),
                    (f"загружен {loaded['time'][:10]}: " if loaded else "сейчас демонстрационный: ")
                    + f"{len(values)} значений, моделей {len({v.model for v in values})}",
                    "«Справочник» → загрузить CSV или Excel (шаблон — пакет партнёра)"))
    prices, _ = prajs.load()
    loaded = _done(actions, "prajs", "prajs_1c")
    out.append(Step("prajs", "Прайс для ТКП", bool(loaded),
                    (f"обновлён {loaded['time'][:10]}" if loaded else "сейчас демонстрационный")
                    + f": цен {len(prices)}", "«Справочник» → «Прайс для ТКП» → загрузить или цены из 1С"))
    if onec.configured():
        rep = onec.last_report()
        out.append(Step("1c", "Цены из 1С", bool(rep), f"последняя загрузка {rep['time']}: цен {rep['prices']}"
                        if rep else "ещё не загружались", "«Справочник» → «Обновить цены из 1С»", optional=True))
    else:
        out.append(Step("1c", "Цены из 1С", None, "не подключена — если прайс ведётся в 1С, подключите её ниже",
                        "«Подключения» ниже", optional=True))
    company = tkp_registry.load_settings().get("company", "")
    blank = tkp_registry.blank()
    out.append(Step("tkp", "Бланк и условия ТКП", bool(company),
                    (f"компания: {company}" if company else "реквизиты компании не заполнены")
                    + ("; бланк Word загружен" if blank else "; бланк — стандартный"),
                    "«Реестр ТКП» → «Бланк и условия»"))
    if settings.mail_host:
        rec = jobs.last("mail") or {}
        out.append(Step("mail", "Почта отдела", bool(rec.get("ok")), rec.get("message") or "ещё не проверялась",
                        "кнопка «Проверить» ниже, затем задачи по расписанию", optional=True))
    else:
        out.append(Step("mail", "Почта отдела", None, "не подключена — письма клиентов можно вставлять в чат "
                        "вручную", "«Подключения» ниже", optional=True))
    from . import tls
    cert = tls.info()
    out.append(Step("https", "HTTPS для чата и API", bool(cert) and cert["days_left"] >= tls.WARN_DAYS,
                    (f"сертификат на {', '.join(cert['names'][:3])} до {cert['until']}"
                     + (" — пора перевыпустить" if cert["days_left"] < tls.WARN_DAYS else "")
                     + ("; сотрудникам — data\\tls\\agent-ca.cer в доверенные" if cert["own_ca"] else ""))
                    if cert else "нет — пароли и вопросы идут по сети открытым текстом",
                    "python scripts\\make_cert.py (или --pfx от службы ИТ), затем перезапустить чат и API"))
    rows = jobs.status(registered=registered)
    if registered is not None:
        missing = [r["title"] for r in rows if r["state"] != "off" and not r["registered"]]
        out.append(Step("jobs", "Задачи по расписанию", not missing, "все настроенные — в Планировщике" if not missing
                        else "нет в Планировщике: " + ", ".join(missing), "scripts\\install_tasks.bat"))
    backup = next(r for r in rows if r["key"] == "backup")
    from .backup import copy_dir, last_copy
    copied = last_copy() if copy_dir() else None
    out.append(Step("backup", "Резервная копия", backup["state"] == "ok",
                    f"последняя — {jobs.ago(backup['age'])}" + (
                        "; копия на другом диске — " + ("проверена" if copied["ok"] else "не записана")
                        if copied else ""), "«Система» → «Создать резервную копию»; дальше — каждый день по расписанию"))
    out.append(Step("backup_copy", "Копия архивов на другом диске", bool(copied and copied["ok"]
                                                                          and not copied.get("same_disk")),
                    ("последняя — " + copied["time"][:16] + (", проверена" if copied["ok"] else f": {copied['error']}"))
                    if copied else ("папка задана, копий ещё не было" if copy_dir() else
                                    "не задана — при отказе диска сервера пропадут и система, и её архивы"),
                    "«Подключения» ниже → «Копия резервных архивов на другом диске»"))
    results = ROOT / "eval" / "results"
    runs = sorted([*results.glob("partner*.md"), *results.glob("priemka*.md")], key=lambda p: p.stat().st_mtime)
    out.append(Step("quality", "Проверка на вопросах предприятия", bool(runs),
                    f"последний прогон: {runs[-1].stem}" if runs else "прогона на вопросах заказчика ещё не было",
                    "python eval\\run_eval.py --questions eval\\partner.yaml --out partner (вопросы — из пакета "
                    "партнёра, scripts\\import_partner.py)"))
    return out


def progress(items: list[Step]) -> tuple[int, int]:
    """Сделано из обязательных: необязательный шаг считается, только если подключение задано."""
    counted = [s for s in items if s.done is not None]
    return sum(1 for s in counted if s.done), len(counted)

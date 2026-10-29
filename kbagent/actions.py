"""Журнал действий — для службы безопасности: кто, что и когда сделал в системе.

Пишутся вход и выход, неудачные попытки входа, загрузка, утверждение и удаление документов,
выдача ключей API и запросы к API с неверным ключом. Файл logs/actions.jsonl только дописывается;
вопросы и ответы сотрудников — в отдельном журнале запросов (logs/audit.jsonl).
"""
import json
import time

from .config import settings

ACTIONS = {
    "login": "Вход",
    "login_failed": "Неудачная попытка входа",
    "login_locked": "Вход закрыт после неудачных попыток",
    "logout": "Выход",
    "upload": "Загрузка документа",
    "approve": "Утверждение документа",
    "remove": "Удаление документа",
    "api_key": "Выдача ключа API",
    "api_denied": "Запрос к API с неверным ключом",
    "api_error": "Сбой ответа через API",
    "api_tkp": "ТКП через API",
    "folder_sync": "Папка документов: изменение",
    "backup": "Резервная копия",
    "tkp_registered": "Регистрация ТКП (номер)",
    "tkp_status": "Статус ТКП",
    "tkp_note": "Отметка по ТКП (напоминание)",
    "user_created": "Пользователь создан",
    "user_updated": "Пользователь изменён",
    "user_deleted": "Пользователь удалён",
    "user_password_reset": "Сброс пароля",
    "user_password_changed": "Пароль сменён пользователем",
    "user_first_run": "Первый запуск: демо-учётки убраны",
    "tkp_settings": "Бланк и условия ТКП",
    "mail_request": "Запрос клиента из почты",
    "mail_status": "Входящий запрос: статус",
    "spravochnik": "Загрузка справочника параметров",
    "prajs": "Загрузка прайса",
    "prajs_1c": "Цены из 1С",
    "pilot_report": "Отчёт о пилоте",
    "expert": "Решение эксперта",
    "expert_sent": "Вопрос передан эксперту",
    "draft_approved": "Утверждение черновика ответа",
    "tkp_approved": "Утверждение ТКП",
}


def _path():
    return settings.log_dir / "actions.jsonl"


def log_action(action: str, user: str | None, target: str = "", details: str = "") -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "user": user or "—",
              "target": target, "details": details}
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_actions(limit: int = 20000) -> list[dict]:
    if not _path().exists():
        return []
    lines = _path().read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]

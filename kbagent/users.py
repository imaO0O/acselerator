"""Учётные записи: создание, роль, сброс пароля, блокировка — без правки кода и скриптов.

Хранятся в data/users.yaml, только хэши паролей (PBKDF2, kbagent/auth.py). Новый пользователь и сброс пароля —
временный пароль, который показывается администратору один раз; при первом входе пользователь задаёт свой.
Демо-учётки прототипа (scripts/make_users.py) имеют пароли из открытого репозитория: пока они активны,
администратор видит предупреждение, а scripts/first_run.py при установке у заказчика их убирает.
Каждое действие — в журнале действий.
"""
import re
import secrets
import time

import yaml

from .actions import log_action
from .auth import end_user_sessions, hash_password, verify_password
from .config import settings

MIN_LENGTH = 10
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# пароли демо-учёток опубликованы в scripts/make_users.py — на установке у заказчика их быть не должно
DEMO_PASSWORDS = {"inzhener": "demo-inzhener-2026", "metrolog": "demo-metrolog-2026",
                  "prodazhi": "demo-prodazhi-2026", "admin": "demo-admin-2026"}


class UserError(ValueError):
    pass


def _load() -> list[dict]:
    if not settings.users_path.exists():
        return []
    return (yaml.safe_load(settings.users_path.read_text(encoding="utf-8")) or {}).get("users", []) or []


def _save(users: list[dict]) -> None:
    settings.users_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.users_path.with_suffix(".tmp")
    tmp.write_text("# Учётные записи чата: только хэши паролей. Управление — «Администрирование» → «Пользователи».\n"
                   + yaml.safe_dump({"users": users}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(settings.users_path)


def _clearance(user: dict) -> int:
    if "clearance" in user:
        return int(user["clearance"])
    # администратор базы знаний работает со всеми документами; остальным — без грифа, пока не назначен допуск
    return len(settings.levels) - 1 if user.get("role") == "Администратор" else 0


def clearance(login: str | None) -> int:
    """Уровень допуска к документам с грифом; неизвестный пользователь (служебный прогон) — без допуска."""
    user = next((u for u in _load() if u["login"] == login), None) if login else None
    return _clearance(user) if user else 0


def listing() -> list[dict]:
    """Пользователи без хэшей паролей — для таблицы администратора."""
    return [{k: v for k, v in u.items() if k != "password_hash"} | {"active": u.get("active", True),
                                                                    "must_change": u.get("must_change", False),
                                                                    "clearance": _clearance(u)}
            for u in _load()]


def check_password(password: str, login: str = "") -> str:
    """Пусто — пароль годится; иначе — что не так."""
    if len(password) < MIN_LENGTH:
        return f"пароль — не короче {MIN_LENGTH} символов"
    if login and login.lower() in password.lower():
        return "пароль не должен содержать логин"
    if password in DEMO_PASSWORDS.values():
        return "этот пароль опубликован в репозитории прототипа"
    if not (re.search(r"[A-Za-zА-Яа-яЁё]", password) and re.search(r"\d", password)):
        return "в пароле нужны буквы и цифры"
    return ""


def temporary_password() -> str:
    """Временный пароль: 12 символов без похожих букв и цифр (l/1, O/0) — его диктуют или передают на бумаге."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(12))
        if re.search(r"\d", p) and re.search(r"[A-Za-z]", p):
            return p


def _find(users: list[dict], login: str) -> dict:
    user = next((u for u in users if u["login"] == login), None)
    if user is None:
        raise UserError(f"пользователя «{login}» нет")
    return user


def add(login: str, name: str, role: str, by: str | None, clearance: int | None = None) -> str:
    """Новый пользователь; возвращает временный пароль — показать администратору один раз."""
    login = login.strip().lower()
    if not LOGIN_RE.match(login):
        raise UserError("логин — латиница, цифры, точка, дефис или подчёркивание, 2–32 символа")
    if role not in settings.roles:
        raise UserError(f"роль — одна из: {', '.join(settings.roles)}")
    users = _load()
    if any(u["login"] == login for u in users):
        raise UserError(f"логин «{login}» уже занят")
    password = temporary_password()
    users.append({"login": login, "name": name.strip() or login, "role": role, "password_hash": hash_password(password),
                  "active": True, "must_change": True, "created": time.strftime("%Y-%m-%d %H:%M")})
    if clearance is not None:
        users[-1]["clearance"] = clearance
    _save(users)
    log_action("user_created", by, login, f"роль: {role}" + (f"; допуск: {settings.levels[clearance]}"
                                                             if clearance else ""))
    return password


def reset_password(login: str, by: str | None) -> str:
    users = _load()
    user = _find(users, login)
    password = temporary_password()
    user.update(password_hash=hash_password(password), must_change=True)
    _save(users)
    end_user_sessions(login)
    log_action("user_password_reset", by, login, "выдан временный пароль")
    return password


def change_password(login: str, old: str, new: str) -> None:
    """Пользователь меняет свой пароль (в том числе временный при первом входе)."""
    users = _load()
    user = _find(users, login)
    if not verify_password(old, user["password_hash"]):
        raise UserError("текущий пароль неверный")
    if problem := check_password(new, login):
        raise UserError(problem)
    if verify_password(new, user["password_hash"]):
        raise UserError("новый пароль совпадает с прежним")
    user.update(password_hash=hash_password(new), must_change=False, changed=time.strftime("%Y-%m-%d %H:%M"))
    _save(users)
    log_action("user_password_changed", login, login, "")


def update(login: str, by: str | None, role: str | None = None, active: bool | None = None, name: str | None = None,
           clearance: int | None = None) -> None:
    users = _load()
    user = _find(users, login)
    if role is not None and role not in settings.roles:
        raise UserError(f"роль — одна из: {', '.join(settings.roles)}")
    admins = [u for u in users if u["role"] == "Администратор" and u.get("active", True)]
    losing_admin = user in admins and ((role is not None and role != "Администратор") or active is False)
    if losing_admin and len(admins) == 1:
        raise UserError("это последний активный администратор — сначала назначьте другого")
    changes = []
    if role is not None and role != user["role"]:
        user["role"] = role
        changes.append(f"роль: {role}")
    if active is not None and active != user.get("active", True):
        user["active"] = active
        changes.append("разблокирован" if active else "заблокирован")
    if name is not None and name.strip() and name.strip() != user["name"]:
        user["name"] = name.strip()
        changes.append(f"имя: {name.strip()}")
    if clearance is not None and clearance != _clearance(user):
        if not 0 <= clearance < len(settings.levels):
            raise UserError("неизвестный уровень допуска")
        user["clearance"] = clearance
        changes.append(f"допуск: {settings.levels[clearance]}")
    if changes:
        _save(users)
        if active is False:
            end_user_sessions(login)
        log_action("user_updated", by, login, "; ".join(changes))


def remove(login: str, by: str | None) -> None:
    users = _load()
    user = _find(users, login)
    admins = [u for u in users if u["role"] == "Администратор" and u.get("active", True)]
    if user in admins and len(admins) == 1:
        raise UserError("это последний активный администратор — сначала назначьте другого")
    _save([u for u in users if u["login"] != login])
    end_user_sessions(login)
    log_action("user_deleted", by, login, "")


def demo_accounts() -> list[str]:
    """Активные учётки, у которых пароль — опубликованный демо-пароль."""
    return [u["login"] for u in _load() if u.get("active", True) and u["login"] in DEMO_PASSWORDS
            and verify_password(DEMO_PASSWORDS[u["login"]], u["password_hash"])]


def first_run(admin_login: str = "admin", admin_name: str = "Администратор") -> str:
    """Установка у заказчика: демо-учётки убираются, создаётся администратор со случайным временным паролем."""
    password = temporary_password()
    users = [u for u in _load() if u["login"] not in DEMO_PASSWORDS]
    users = [u for u in users if u["login"] != admin_login]
    users.append({"login": admin_login, "name": admin_name, "role": "Администратор",
                  "password_hash": hash_password(password), "active": True, "must_change": True,
                  "created": time.strftime("%Y-%m-%d %H:%M")})
    _save(users)
    log_action("user_first_run", None, admin_login, "демо-учётки удалены, создан администратор")
    return password

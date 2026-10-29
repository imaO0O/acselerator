"""Вход по логину и паролю, сеансы, история диалогов и оценки ответов — всё хранится на сервере.

Пароли хранятся только в виде хэшей PBKDF2-SHA256 (data/users.yaml; управление — kbagent/users.py,
«Администрирование» → «Пользователи»; демо-учётки — scripts/make_users.py). В пилоте вход заменяется учётными записями предприятия
(Active Directory / LDAP): роль берётся из группы пользователя, остальное не меняется.
"""
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

import yaml

from .config import settings

ITERATIONS = 200_000
SESSION_TTL = 12 * 3600          # сеанс живёт 12 часов
LOCK_AFTER = 5                   # неудачных попыток подряд — и вход под этим логином закрывается
LOCK_SECONDS = 300               # на 5 минут
HISTORY_LIMIT = 100              # сколько реплик хранить в одном диалоге


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def load_users() -> dict[str, dict]:
    if not settings.users_path.exists():
        return {}
    users = yaml.safe_load(settings.users_path.read_text(encoding="utf-8")).get("users", [])
    return {u["login"]: u for u in users}


_DUMMY_HASH: list[str] = []


def _failures_path() -> Path:
    return settings.log_dir / "login_failures.json"


def _read_failures() -> dict:
    try:
        return json.loads(_failures_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def locked_for(login: str) -> int:
    """Сколько секунд вход под этим логином ещё закрыт после череды неудачных попыток (0 — открыт)."""
    rec = _read_failures().get(login.strip().lower(), {})
    return max(0, int(rec.get("until", 0) - time.time()))


def _register(login: str, ok: bool) -> None:
    failures = _read_failures()
    if ok:
        failures.pop(login, None)
    else:
        rec = failures.get(login, {"count": 0})
        if rec.get("until", 0) and rec["until"] <= time.time():
            rec = {"count": 0}                    # прошлая блокировка истекла — считаем заново
        rec["count"] += 1
        if rec["count"] >= LOCK_AFTER:
            rec["until"] = time.time() + LOCK_SECONDS
        failures[login] = rec
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    _failures_path().write_text(json.dumps(failures), encoding="utf-8")


def authenticate(login: str, password: str) -> dict | None:
    """Пользователь или None. После LOCK_AFTER неудач подряд логин закрыт на LOCK_SECONDS, даже с верным паролем.
    Для неизвестного логина пароль тоже сверяется с хэшем, чтобы по времени ответа нельзя было узнать,
    какие логины существуют."""
    login = login.strip().lower()
    if locked_for(login):
        return None
    user = load_users().get(login)
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_hex(8)))
    ok = (verify_password(password, user["password_hash"] if user else _DUMMY_HASH[0]) and user is not None
          and user.get("active", True))                      # заблокированный — как неверный пароль
    _register(login, ok)
    return _public(user) if ok else None


def _public(user: dict) -> dict:
    return {"login": user["login"], "name": user["name"], "role": user["role"],
            "must_change": user.get("must_change", False)}


# ---------- сеансы ----------

def _sessions_path():
    return settings.log_dir / "sessions.json"


def _read_sessions() -> dict:
    try:
        return json.loads(_sessions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_sessions(sessions: dict) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    sessions = {t: s for t, s in sessions.items() if now - s["created"] < SESSION_TTL}
    _sessions_path().write_text(json.dumps(sessions), encoding="utf-8")


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    sessions = _read_sessions()
    sessions[token] = {"login": user["login"], "created": time.time()}
    _write_sessions(sessions)
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    s = _read_sessions().get(token)
    if not s or time.time() - s["created"] >= SESSION_TTL:
        return None
    u = load_users().get(s["login"])
    return _public(u) if u and u.get("active", True) else None     # заблокирован или удалён — сеанс недействителен


def end_user_sessions(login: str) -> None:
    """Все сеансы пользователя — после блокировки или сброса пароля."""
    sessions = {t: s for t, s in _read_sessions().items() if s["login"] != login}
    _write_sessions(sessions)


def end_session(token: str | None) -> None:
    sessions = _read_sessions()
    if token in sessions:
        del sessions[token]
        _write_sessions(sessions)


# ---------- диалоги ----------

def _safe(login: str) -> str:
    return re.sub(r"[^\w\-]", "_", login)


def _conv_dir(login: str) -> Path:
    return settings.log_dir / "history" / _safe(login)


def new_conversation() -> dict:
    now = time.strftime("%Y-%m-%d %H:%M")
    return {"id": time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3), "title": "Новый диалог",
            "created": now, "updated": now, "items": []}


def _migrate_legacy(login: str) -> None:
    """История одного диалога из прежней версии (logs/history/<логин>.json) становится первым диалогом."""
    legacy = settings.log_dir / "history" / f"{_safe(login)}.json"
    if not legacy.exists():
        return
    try:
        items = json.loads(legacy.read_text(encoding="utf-8"))
    except ValueError:
        items = []
    if items:
        conv = new_conversation()
        conv.update(title=items[0]["question"][:60], items=items)
        save_conversation(login, conv)
    legacy.unlink()


def list_conversations(login: str) -> list[dict]:
    _migrate_legacy(login)
    out = []
    for path in _conv_dir(login).glob("*.json"):
        try:
            conv = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        questions = " ".join(i["question"] for i in conv.get("items", []))
        out.append({"id": conv["id"], "title": conv["title"], "updated": conv["updated"],
                    "count": len(conv.get("items", [])), "questions": questions})
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def load_conversation(login: str, conv_id: str) -> dict | None:
    try:
        return json.loads((_conv_dir(login) / f"{_safe(conv_id)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_conversation(login: str, conv: dict) -> None:
    items = conv["items"]
    if items and (conv["title"] == "Новый диалог" or conv.get("title_info")):
        # название — по первому настоящему вопросу, а не по «Привет» или «Что ты умеешь?»
        real = next((i for i in items if i.get("status") != "info"), None)
        conv["title"] = (real or items[0])["question"][:60]
        conv["title_info"] = real is None
    conv["updated"] = time.strftime("%Y-%m-%d %H:%M")
    conv["items"] = conv["items"][-HISTORY_LIMIT:]
    path = _conv_dir(login) / f"{_safe(conv['id'])}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")


def delete_conversation(login: str, conv_id: str) -> None:
    (_conv_dir(login) / f"{_safe(conv_id)}.json").unlink(missing_ok=True)


# ---------- оценки ответов ----------

def save_feedback(record: dict) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    from .privacy import mask
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
              **{k: mask(v) if k in ("question", "answer", "comment") else v for k, v in record.items()}}
    with open(settings.log_dir / "feedback.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_feedback() -> list[dict]:
    path = settings.log_dir / "feedback.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

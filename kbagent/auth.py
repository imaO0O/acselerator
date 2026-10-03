"""Вход по логину и паролю, сеансы, история диалогов и оценки ответов — всё хранится на сервере.

Пароли хранятся только в виде хэшей PBKDF2-SHA256 (data/users.yaml, создаётся
scripts/make_users.py). В пилоте вход заменяется учётными записями предприятия
(Active Directory / LDAP): роль берётся из группы пользователя, остальное не меняется.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

import yaml

from .config import settings

ITERATIONS = 200_000
SESSION_TTL = 12 * 3600          # сеанс живёт 12 часов
HISTORY_LIMIT = 50               # сколько реплик диалога хранить на пользователя


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


def authenticate(login: str, password: str) -> dict | None:
    user = load_users().get(login.strip().lower())
    if user and verify_password(password, user["password_hash"]):
        return {"login": user["login"], "name": user["name"], "role": user["role"]}
    return None


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
    return {"login": u["login"], "name": u["name"], "role": u["role"]} if u else None


def end_session(token: str | None) -> None:
    sessions = _read_sessions()
    if token in sessions:
        del sessions[token]
        _write_sessions(sessions)


# ---------- история диалога ----------

def _history_path(login: str):
    safe = re.sub(r"[^\w\-]", "_", login)
    return settings.log_dir / "history" / f"{safe}.json"


def load_history(login: str) -> list[dict]:
    try:
        return json.loads(_history_path(login).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_history(login: str, history: list[dict]) -> None:
    path = _history_path(login)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history[-HISTORY_LIMIT:], ensure_ascii=False), encoding="utf-8")


# ---------- оценки ответов ----------

def save_feedback(record: dict) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
    with open(settings.log_dir / "feedback.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_feedback() -> list[dict]:
    path = settings.log_dir / "feedback.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

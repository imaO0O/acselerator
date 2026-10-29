"""Учётные записи: создание, временный пароль, смена, блокировка, последний администратор, демо-учётки."""
import pytest

from kbagent import auth, users
from kbagent.config import settings


@pytest.fixture
def store(tmp_path, monkeypatch, tmp_settings):
    monkeypatch.setattr(settings, "users_path", tmp_path / "users.yaml")
    return users


def test_user_lifecycle_with_temporary_password(store):
    admin_pwd = store.first_run()
    admin = auth.authenticate("admin", admin_pwd)
    assert admin and admin["must_change"] and store.demo_accounts() == []
    temp = store.add("Ivanov", "Иванов И. И.", "Техпродажи и сервис", "admin")
    user = auth.authenticate("ivanov", temp)
    assert user["role"] == "Техпродажи и сервис" and user["must_change"]
    with pytest.raises(store.UserError, match="не короче"):
        store.change_password("ivanov", temp, "short1")
    with pytest.raises(store.UserError, match="опубликован"):
        store.change_password("ivanov", temp, "demo-admin-2026")
    with pytest.raises(store.UserError, match="текущий пароль"):
        store.change_password("ivanov", "wrong", "Новый-пароль-2027")
    store.change_password("ivanov", temp, "Новый-пароль-2027")
    assert auth.authenticate("ivanov", "Новый-пароль-2027")["must_change"] is False

    token = auth.create_session({"login": "ivanov"})
    store.update("ivanov", "admin", active=False)                   # блокировка — сеанс недействителен сразу
    assert auth.session_user(token) is None and auth.authenticate("ivanov", "Новый-пароль-2027") is None
    store.update("ivanov", "admin", active=True, role="Метролог")
    assert auth.authenticate("ivanov", "Новый-пароль-2027")["role"] == "Метролог"
    again = store.reset_password("ivanov", "admin")
    assert auth.authenticate("ivanov", again)["must_change"] and auth.authenticate("ivanov", "Новый-пароль-2027") is None
    with pytest.raises(store.UserError, match="занят"):
        store.add("ivanov", "Дубль", "Метролог", "admin")
    with pytest.raises(store.UserError, match="логин"):
        store.add("иванов", "Кириллица", "Метролог", "admin")


def test_last_admin_is_protected_and_demo_accounts_are_found(store, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("make_users", Path(__file__).parents[1] / "scripts" / "make_users.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    assert dict((login, pwd) for login, _, _, pwd in mk.DEMO_USERS) == store.DEMO_PASSWORDS   # список не разошёлся
    mk.main()
    assert sorted(store.demo_accounts()) == ["admin", "inzhener", "metrolog", "prodazhi"]
    with pytest.raises(store.UserError, match="последний"):
        store.update("admin", "admin", role="Метролог")
    with pytest.raises(store.UserError, match="последний"):
        store.remove("admin", "admin")
    store.first_run("chief", "Главный администратор")
    assert store.demo_accounts() == [] and {u["login"] for u in store.listing()} == {"chief"}

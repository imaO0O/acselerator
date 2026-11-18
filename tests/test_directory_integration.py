import pytest

from kbagent import auth, directory_settings
from kbagent.directory_auth import DirectoryConfigurationError, DirectoryIdentity


def test_directory_login_sessions_fail_closed_and_expire(tmp_settings, monkeypatch):
    from kbagent import directory_auth
    monkeypatch.setenv('KB_AUTH_PROVIDER', 'ldap')
    monkeypatch.setattr(directory_settings, 'load', lambda: object())
    monkeypatch.setattr(directory_auth, 'authenticate', lambda *a: DirectoryIdentity('Ivan', 'Иван', 'Техпродажи и сервис', 'cn=Ivan'))
    user = auth.authenticate('Ivan', 'secret')
    assert user['login'] == 'ldap:ivan'
    token = auth.create_session(user)
    assert auth.session_user(token)['provider'] == 'ldap'
    now = auth.time.time()
    monkeypatch.setattr(auth.time, 'time', lambda: now + 301)
    assert auth.session_user(token) is None
    monkeypatch.setattr(directory_auth, 'authenticate', lambda *a: None)
    assert auth.authenticate('admin', 'demo-admin-2026') is None


def test_invalid_directory_config_never_uses_local_password(tmp_settings, monkeypatch):
    monkeypatch.setenv('KB_AUTH_PROVIDER', 'ldap')
    def broken():
        raise DirectoryConfigurationError('broken')
    monkeypatch.setattr(directory_settings, 'load', broken)
    monkeypatch.setattr(auth, 'load_users', lambda: pytest.fail('local fallback is forbidden'))
    assert auth.authenticate('admin', 'correct-local-password') is None


def test_changing_provider_invalidates_existing_local_session(tmp_settings, monkeypatch):
    monkeypatch.setattr(auth, 'load_users', lambda: {'u': {'login':'u','name':'User','role':'Администратор'}})
    token = auth.create_session({'login':'u'})
    assert auth.session_user(token)
    monkeypatch.setenv('KB_AUTH_PROVIDER', 'ldap')
    assert auth.session_user(token) is None


def test_ldap_history_isolation(tmp_settings):
    owner = 'ldap:user.name'
    other = 'ldap:user_name'
    conversation = auth.new_conversation()
    conversation['items'] = [{'question': 'Запрос первого сотрудника', 'text': 'Ответ'}]
    auth.save_conversation(owner, conversation)
    assert auth.load_conversation(owner, conversation['id']) is not None
    assert auth.load_conversation(other, conversation['id']) is None

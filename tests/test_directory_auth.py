"""All directory operations are mocked; no ldap3 dependency or live binds."""
from dataclasses import replace
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kbagent import directory_auth as directory


@pytest.fixture
def config(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("mock CA file; no TLS connection is made")
    return directory.DirectoryConfig(host="dc.example.test", ca_file=ca,
        base_dn="DC=example,DC=test", bind_dn="CN=reader,DC=example,DC=test",
        bind_password="service-secret", group_roles={"CN=Engineers,DC=example,DC=test": "Метролог"},
        allowed_roles=frozenset({"Метролог", "Администратор"}),
        denied_groups=frozenset({"CN=Denied,DC=example,DC=test"}))


@pytest.fixture
def backend(monkeypatch):
    entry = {"type": "searchResEntry", "dn": "CN=Ivan,DC=example,DC=test", "attributes": {
        "sAMAccountName": ["ivan"], "displayName": ["Иван"],
        "memberOf": ["CN=Engineers,DC=example,DC=test"]}}
    service = Mock(result={"result": 0}, response=[entry])
    service.bind.return_value = True
    service.search.return_value = True
    user = Mock(result={"result": 0})
    user.bind.return_value = True
    ldap = SimpleNamespace(Tls=Mock(), Server=Mock(), Connection=Mock(side_effect=[service, user]),
                           NONE="NONE", SIMPLE="SIMPLE", SUBTREE="SUBTREE")
    def escape(text):
        for raw, safe in (("\\", "\\5c"), ("*", "\\2a"), ("(", "\\28"), (")", "\\29"), ("\x00", "\\00")):
            text = text.replace(raw, safe)
        return text
    escape_mock = Mock(side_effect=escape)
    monkeypatch.setattr(directory, "_load_ldap", lambda: (ldap, escape_mock))
    return ldap, service, user, entry, escape_mock


def test_success_explicit_tls_and_bind_contract(config, backend):
    ldap, service, user, entry, escape = backend
    identity = directory.authenticate(config, "ivan", "user-secret")
    assert identity == directory.DirectoryIdentity("ivan", "Иван", "Метролог", entry["dn"])
    tls = ldap.Tls.call_args.kwargs
    assert tls["validate"] == ssl.CERT_REQUIRED and tls["ca_certs_file"] == str(config.ca_file)
    assert tls["valid_names"] == [config.host] and tls["sni"] == config.host
    assert ldap.Server.call_args.kwargs["use_ssl"] is True
    assert ldap.Connection.call_count == 2
    for call in ldap.Connection.call_args_list:
        assert call.kwargs["auto_referrals"] is False
        assert call.kwargs["authentication"] == "SIMPLE"
        assert call.kwargs["read_only"] is True and call.kwargs["raise_exceptions"] is True
    assert ldap.Connection.call_args_list[1].kwargs["user"] == entry["dn"]
    assert ldap.Connection.call_args_list[1].kwargs["password"] == "user-secret"
    service.unbind.assert_called_once()
    user.unbind.assert_called_once()
    assert "service-secret" not in repr(config)


def test_filter_uses_escaped_username(config, backend):
    ldap, service, user, entry, escape = backend
    directory.authenticate(config, "a*)(uid=*)", "secret")
    escape.assert_called_once_with("a*)(uid=*)")
    assert service.search.call_args.args[1] == r"(sAMAccountName=a\2a\29\28uid=\2a\29)"
    assert service.search.call_args.kwargs["size_limit"] == 2


@pytest.mark.parametrize("username,password", [("", "x"), ("ivan", ""), ("ivan", None),
                                               ("\x00", "x"), ("a" * 257, "x")])
def test_no_empty_or_anonymous_binds(config, backend, username, password):
    ldap = backend[0]
    assert directory.authenticate(config, username, password) is None
    ldap.Connection.assert_not_called()


@pytest.mark.parametrize("groups", [[], ["CN=Unmapped"], ["CN=Engineers,DC=example,DC=test", "CN=Denied,DC=example,DC=test"]])
def test_group_denial_never_authenticates(config, backend, groups):
    ldap, service, user, entry, _ = backend
    entry["attributes"]["memberOf"] = groups
    assert directory.authenticate(config, "ivan", "secret") is None
    assert ldap.Connection.call_count == 1


def test_conflicting_roles_deny_and_case_insensitive_group_allowlist(config, backend):
    entry = backend[3]
    entry["attributes"]["memberOf"] = ["cn=engineers,dc=example,dc=test", "CN=Admins"]
    config = replace(config, group_roles={**config.group_roles, "CN=Admins": "Администратор"})
    assert directory.authenticate(config, "ivan", "secret") is None


def test_case_insensitive_groups_succeed(config, backend):
    backend[3]["attributes"]["memberOf"] = ["cn=engineers,dc=example,dc=test"]
    assert directory.authenticate(config, "ivan", "secret").role == "Метролог"


@pytest.mark.parametrize("failure", ["service_bind", "user_bind", "search", "tls", "referral", "multiple", "missing_groups", "partial"])
def test_fail_closed_and_cleanup(config, backend, failure):
    ldap, service, user, entry, _ = backend
    if failure == "service_bind":
        service.bind.return_value = False
    elif failure == "user_bind":
        user.bind.return_value = False
    elif failure == "search":
        service.search.side_effect = RuntimeError("directory unavailable; secret")
    elif failure == "tls":
        service.bind.side_effect = ssl.SSLCertVerificationError("bad hostname")
    elif failure == "referral":
        service.response.append({"type": "searchResRef", "uri": "ldap://plaintext"})
    elif failure == "multiple":
        service.response.append(entry.copy())
    elif failure == "missing_groups":
        del entry["attributes"]["memberOf"]
    elif failure == "partial":
        service.search.side_effect = lambda *a, **k: service.result.update(result=4) or True
    assert directory.authenticate(config, "ivan", "secret") is None
    service.unbind.assert_called_once()
    if ldap.Connection.call_count == 2:
        user.unbind.assert_called_once()


@pytest.mark.parametrize("changes", [{"host": "ldap://dc.example.test"}, {"host": "ldaps://dc.example.test"},
    {"host": "*.example.test"}, {"host": "dc.example.test:389"}, {"port": 0}, {"timeout": 0},
    {"bind_password": ""}, {"group_roles": {}}, {"group_roles": {"CN=Group": "Unknown"}},
    {"username_attribute": "uid)(objectClass=*"}, {"allowed_roles": frozenset()}])
def test_bad_config_never_opens_connection(config, backend, changes):
    with pytest.raises(directory.DirectoryConfigurationError):
        directory.authenticate(replace(config, **changes), "ivan", "secret")
    backend[0].Connection.assert_not_called()


def test_missing_ca_and_lazy_dependency(config, monkeypatch):
    with pytest.raises(directory.DirectoryConfigurationError):
        directory.authenticate(replace(config, ca_file=config.ca_file.parent / "missing"), "ivan", "secret")
    import builtins
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name.startswith("ldap3"):
            raise ImportError()
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(directory.DirectoryConfigurationError, match="optional ldap3"):
        directory.authenticate(config, "ivan", "secret")

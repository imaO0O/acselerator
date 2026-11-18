"""Standalone, opt-in LDAPS authentication. No auth/session integration.

API: authenticate(config: DirectoryConfig, username: str, password: str)
     -> DirectoryIdentity | None.
None means DENY (including directory/TLS errors); NEVER fall back to local auth.
Invalid configuration or missing optional ldap3 raises DirectoryConfigurationError.
Callers must deny on that exception too. Install ldap3 only on directory-enabled
deployments. No network/dependency imports occur when this module is imported.

Search uses a configured service account, then verifies the user's password with
a separate LDAPS bind to the returned DN. Only direct memberOf-style membership
is supported, not nested/primary groups. Deny groups override allow groups; zero
or multiple distinct mapped roles deny. Unmapped groups confer no permissions.
Group DNs match conservatively, case-insensitively; no DN equivalence guessing.
The caller owns rate limiting, sessions, revocation/revalidation and audit events.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
import ssl
from typing import Mapping


class DirectoryConfigurationError(ValueError):
    """Directory login unavailable; callers must deny without local fallback."""


@dataclass(frozen=True)
class DirectoryConfig:
    host: str
    ca_file: Path
    base_dn: str
    bind_dn: str
    bind_password: str = field(repr=False)
    group_roles: Mapping[str, str]
    allowed_roles: frozenset[str]
    denied_groups: frozenset[str] = frozenset()
    port: int = 636
    timeout: int = 5
    username_attribute: str = "sAMAccountName"
    display_attribute: str = "displayName"
    group_attribute: str = "memberOf"

    def validate(self):
        # ldap3 accepts URLs in host and lets them override use_ssl/port. Disallow
        # URLs, wildcards and host:port forms so configuration cannot downgrade.
        if (not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", self.host)
                or ".." in self.host):
            raise DirectoryConfigurationError("Configure a bare DNS host or IPv4 address")
        if not Path(self.ca_file).is_file():
            raise DirectoryConfigurationError("An explicit CA certificate file is required")
        if not self.base_dn.strip() or not self.bind_dn.strip() or not self.bind_password:
            raise DirectoryConfigurationError("Base DN and nonempty service credentials are required")
        if (type(self.port) is not int or not 1 <= self.port <= 65535
                or type(self.timeout) is not int or not 1 <= self.timeout <= 60):
            raise DirectoryConfigurationError("Invalid LDAPS port or timeout")
        for attribute in (self.username_attribute, self.display_attribute, self.group_attribute):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", attribute):
                raise DirectoryConfigurationError("Invalid LDAP attribute name")
        if not self.group_roles or not self.allowed_roles or any(not r for r in self.allowed_roles):
            raise DirectoryConfigurationError("Explicit group and role allowlists are required")
        folded = set()
        for group, role in self.group_roles.items():
            if not isinstance(group, str) or not group.strip() or role not in self.allowed_roles:
                raise DirectoryConfigurationError("Every group must map to an allowed role")
            key = group.casefold()
            if key in folded:
                raise DirectoryConfigurationError("Duplicate case-insensitive group mapping")
            folded.add(key)
        if any(not isinstance(g, str) or not g.strip() for g in self.denied_groups):
            raise DirectoryConfigurationError("Invalid denied group")


@dataclass(frozen=True)
class DirectoryIdentity:
    login: str
    name: str
    role: str
    dn: str


def _load_ldap():
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        raise DirectoryConfigurationError("Directory login requires optional ldap3") from None
    return ldap3, escape_filter_chars


def authenticate(config: DirectoryConfig, username: str, password: str) -> DirectoryIdentity | None:
    """Return a verified identity or deny; never log credentials or auto-provision.

    allowed_roles should be frozenset(settings.roles) in the integrating app.
    Caller must explicitly translate this identity into its session representation.
    """
    config.validate()
    if (not isinstance(username, str) or not username.strip() or len(username) > 256
            or "\x00" in username or not isinstance(password, str) or not password or "\x00" in password):
        return None  # In particular, never perform an empty-password/anonymous bind.
    ldap, escape = _load_ldap()
    connections = []
    try:
        tls = ldap.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=str(config.ca_file),
                       valid_names=[config.host], sni=config.host,
                       ssl_options=[ssl.OP_NO_TLSv1, ssl.OP_NO_TLSv1_1])
        server = ldap.Server(config.host, port=config.port, use_ssl=True, tls=tls,
                             get_info=ldap.NONE, connect_timeout=config.timeout)

        def bind(dn, secret):
            conn = ldap.Connection(server, user=dn, password=secret, authentication=ldap.SIMPLE,
                                   auto_bind=False, auto_referrals=False, raise_exceptions=True,
                                   read_only=True, receive_timeout=config.timeout)
            connections.append(conn)
            if not conn.bind() or conn.result.get("result") != 0:
                return None
            return conn

        service = bind(config.bind_dn, config.bind_password)
        if service is None:
            return None
        query = f"({config.username_attribute}={escape(username.strip())})"
        attributes = [config.username_attribute, config.display_attribute, config.group_attribute]
        if not service.search(config.base_dn, query, search_scope=ldap.SUBTREE,
                              attributes=attributes, size_limit=2, time_limit=config.timeout):
            return None
        if service.result.get("result") != 0:
            return None  # Includes referral, timeout and size-limit partial results.
        response = service.response
        if len(response) != 1 or response[0].get("type") != "searchResEntry":
            return None
        entry = response[0]
        dn = entry.get("dn")
        attrs = {key.casefold(): value for key, value in entry.get("attributes", {}).items()}

        def values(attribute):
            value = attrs.get(attribute.casefold(), [])
            return [value] if isinstance(value, str) else value

        logins = values(config.username_attribute)
        groups = values(config.group_attribute)
        names = values(config.display_attribute)
        if (not isinstance(dn, str) or not dn or not isinstance(logins, list) or len(logins) != 1
                or not isinstance(logins[0], str) or not logins[0]
                or not isinstance(groups, list) or any(not isinstance(g, str) for g in groups)):
            return None
        memberships = {g.casefold() for g in groups}
        if memberships & {g.casefold() for g in config.denied_groups}:
            return None
        roles = {role for group, role in config.group_roles.items() if group.casefold() in memberships}
        if len(roles) != 1:
            return None
        if bind(dn, password) is None:
            return None
        name = names[0] if isinstance(names, list) and names and isinstance(names[0], str) else logins[0]
        return DirectoryIdentity(login=logins[0], name=name, role=roles.pop(), dn=dn)
    except Exception:
        # Directory/TLS/protocol failures never become an authenticated identity.
        # Deliberately suppress exception strings, which may contain directory data.
        return None
    finally:
        for conn in reversed(connections):
            try:
                conn.unbind()
            except Exception:
                pass

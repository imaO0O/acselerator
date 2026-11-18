"""LDAPS configuration from a local administrator-controlled JSON and secret file."""
import json
import os
from pathlib import Path

from .config import settings
from .directory_auth import DirectoryConfig, DirectoryConfigurationError


def enabled() -> bool:
    return os.environ.get('KB_AUTH_PROVIDER', 'local').lower() == 'ldap'


def load() -> DirectoryConfig:
    try:
        path = Path(os.environ['KB_LDAP_CONFIG'])
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        # Password never goes into the settings JSON, history or session.
        secret_path = Path(raw.pop('bind_password_file'))
        if not secret_path.is_absolute():
            secret_path = path.parent / secret_path
        ca = Path(raw.pop('ca_file'))
        if not ca.is_absolute():
            ca = path.parent / ca
        config = DirectoryConfig(**raw, ca_file=ca, bind_password=secret_path.read_text(encoding='utf-8').strip(),
                                 allowed_roles=frozenset(settings.roles))
        config.validate()
        return config
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise DirectoryConfigurationError('Проверьте локальную конфигурацию LDAPS и файл секрета') from e

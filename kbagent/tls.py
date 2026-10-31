"""HTTPS для чата и REST API: сертификат и ключ — data/tls/cert.pem и key.pem (или KB_TLS_CERT, KB_TLS_KEY).

Без HTTPS по сети предприятия открытым текстом идут пароли при входе, токен сеанса, вопросы и ответы. Лучше всего —
сертификат от удостоверяющего центра предприятия: служба ИТ выдаёт PFX, scripts/make_cert.py --pfx переводит его в
PEM. Для пилота — свой центр сертификации агента (scripts/make_cert.py): он подписывает сертификат на имя и адреса
сервера; его сертификат data/tls/agent-ca.cer один раз ставится в «Доверенные корневые центры сертификации» на
компьютерах сотрудников (или групповой политикой), и браузер перестаёт предупреждать.

Центр агента ограничен именами и адресами сервера (Name Constraints): даже с украденным ключом центра нельзя
выпустить сертификат, которому браузеры сотрудников поверят, для другого сайта — почты, портала, банка. Без
ограничения корневой сертификат в доверенных означал бы возможность перехватывать любой HTTPS сотрудников.
Перевыпуск на те же имена (продление срока) — без повторной раздачи; новое имя или адрес — новый центр: его
agent-ca.cer раздаётся заново, прежний удаляется из доверенных. Ключ центра в резервную копию не входит.
"""
import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

from .config import ROOT

DIR = ROOT / "data" / "tls"
CERT, KEY = DIR / "cert.pem", DIR / "key.pem"
CA_CERT, CA_KEY, CA_PUBLIC = DIR / "ca.pem", DIR / "ca-key.pem", DIR / "agent-ca.cer"
SERVER_DAYS, CA_DAYS = 730, 3650
WARN_DAYS = 30                          # меньше — пора перевыпустить


def paths() -> tuple[Path, Path] | None:
    cert = Path(os.environ.get("KB_TLS_CERT") or CERT)
    key = Path(os.environ.get("KB_TLS_KEY") or KEY)
    return (cert, key) if cert.exists() and key.exists() else None


def default_names() -> tuple[list[str], list[str]]:
    """Имя компьютера, полное имя в домене и адреса IPv4 — по ним сотрудники открывают чат."""
    host = socket.gethostname()
    names = list(dict.fromkeys(n for n in (host, socket.getfqdn(), "localhost") if n))
    try:
        ips = socket.gethostbyname_ex(host)[2]
    except OSError:
        ips = []
    return names, list(dict.fromkeys(["127.0.0.1", *ips]))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _constraints(names: list[str], ips: list[str]):
    """Центр может подписывать только эти имена и адреса — ничего другого браузер от него не примет."""
    from cryptography import x509
    return x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(n) for n in names]
        + [x509.IPAddress(ipaddress.ip_network(f"{i}/{32 if ipaddress.ip_address(i).version == 4 else 128}"))
           for i in ips], excluded_subtrees=None)


def ca_constrained(cert=None) -> tuple[set, set] | None:
    """Имена и адреса, которыми ограничен центр агента; None — центр без ограничения (выпущен до 31.10.2026)."""
    from cryptography import x509
    if cert is None:
        if not CA_CERT.exists():
            return None
        cert = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    try:
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return None
    subtrees = nc.permitted_subtrees or []
    return ({s.value.lower() for s in subtrees if isinstance(s, x509.DNSName)},
            {str(s.value.network_address) for s in subtrees if isinstance(s, x509.IPAddress)})


def _ca(names: list[str], ips: list[str]):
    """Центр сертификации агента, ограниченный этими именами и адресами. Прежний центр годится, если он ограничен и
    покрывает их все; иначе — новый (его agent-ca.cer раздаётся заново)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    if CA_CERT.exists() and CA_KEY.exists():
        cert = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
        allowed = ca_constrained(cert)
        if allowed and {n.lower() for n in names} <= allowed[0] and set(ips) <= allowed[1]:
            return cert, serialization.load_pem_private_key(CA_KEY.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    # в CN не больше 64 байт: кириллица — в названии организации
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"KBAgent CA {socket.gethostname()}"[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AI-агент базы знаний")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(_constraints(names, ips), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write(CA_KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    _write(CA_CERT, cert.public_bytes(serialization.Encoding.PEM))
    _write(CA_PUBLIC, cert.public_bytes(serialization.Encoding.DER))
    return cert, key


def issue(names: list[str], ips: list[str], days: int = SERVER_DAYS) -> Path:
    """Сертификат сервера на эти имена и адреса, подписанный центром агента."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    if not names and not ips:
        raise ValueError("нужно хотя бы одно имя или адрес сервера")
    ca_cert, ca_key = _ca(names, ips)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"KBAgent: {(names or ips)[0]}"[:64])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write(KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    _write(CERT, cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    return CERT


def from_pfx(data: bytes, password: str | None) -> Path:
    """PFX от службы ИТ (сертификат удостоверяющего центра предприятия) → cert.pem с цепочкой и key.pem."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
    except ValueError as e:
        raise ValueError("PFX не открылся — неверный пароль или файл повреждён") from e
    if key is None or cert is None:
        raise ValueError("в PFX нет закрытого ключа или сертификата — попросите службу ИТ выгрузить их вместе")
    _write(KEY, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    _write(CERT, b"".join(c.public_bytes(serialization.Encoding.PEM) for c in [cert, *(chain or [])]))
    return CERT


def info(path: Path | None = None) -> dict | None:
    """Что установлено: на какие имена, до какой даты, чей (центра агента или предприятия)."""
    from cryptography import x509
    found = paths()
    path = path or (found[0] if found else None)
    if path is None or not path.exists():
        return None
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    try:
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = alt.get_values_for_type(x509.DNSName) + [str(i) for i in alt.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    until = cert.not_valid_after_utc
    own = CA_CERT.exists() and x509.load_pem_x509_certificate(CA_CERT.read_bytes()).subject == cert.issuer
    return {"names": names, "until": until.strftime("%d.%m.%Y"),
            "days_left": (until - dt.datetime.now(dt.timezone.utc)).days,
            "issuer": cert.issuer.rfc4514_string(), "own_ca": own,
            "ca_unconstrained": own and ca_constrained() is None}


def remove() -> None:
    """Вернуть HTTP: сертификат и ключ сервера убираются, центр агента остаётся (раздавать заново не придётся)."""
    for p in (CERT, KEY):
        p.unlink(missing_ok=True)


def streamlit_flags(args: list[str]) -> list[str]:
    """Флаги HTTPS для запуска чата, если сертификат есть и флаги не заданы вручную."""
    found = paths()
    if not found or any(a.startswith("--server.sslCertFile") for a in args):
        return []
    return ["--server.sslCertFile", str(found[0]), "--server.sslKeyFile", str(found[1])]

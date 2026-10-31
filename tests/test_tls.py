"""HTTPS: свой центр сертификации, сертификат сервера, PFX от службы ИТ, флаги запуска чата."""
import socket
import ssl
import threading

import pytest

from kbagent import tls


@pytest.fixture
def tls_dir(tmp_path, monkeypatch):
    for name in ("CERT", "KEY", "CA_CERT", "CA_KEY", "CA_PUBLIC"):
        monkeypatch.setattr(tls, name, tmp_path / getattr(tls, name).name)
    monkeypatch.delenv("KB_TLS_CERT", raising=False)
    monkeypatch.delenv("KB_TLS_KEY", raising=False)
    return tmp_path


def test_issued_certificate_passes_real_tls_handshake(tls_dir):
    assert tls.paths() is None and tls.info() is None and tls.streamlit_flags([]) == []
    tls.issue(["localhost", "agent.zavod.local"], ["127.0.0.1"])
    info = tls.info()
    assert info["names"] == ["localhost", "agent.zavod.local", "127.0.0.1"] and info["own_ca"]
    assert 725 <= info["days_left"] <= 730 and tls.CA_PUBLIC.exists()

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tls.CERT, tls.KEY)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as s:
            s.sendall(b"ok")

    threading.Thread(target=serve, daemon=True).start()
    client_ctx = ssl.create_default_context(cafile=str(tls.CA_CERT))   # как браузер с agent-ca.cer в доверенных
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with client_ctx.wrap_socket(raw, server_hostname="agent.zavod.local") as s:
            assert s.recv(2) == b"ok"
    listener.close()


def test_reissue_keeps_agent_ca_only_for_same_names(tls_dir):
    tls.issue(["localhost"], ["127.0.0.1"])
    ca = tls.CA_CERT.read_bytes()
    tls.issue(["localhost"], ["127.0.0.1"], days=365)                    # продление — без повторной раздачи
    assert tls.CA_CERT.read_bytes() == ca
    tls.issue(["agent.zavod.local"], ["10.0.0.15"])                      # новое имя — новый центр
    assert tls.CA_CERT.read_bytes() != ca and tls.info()["names"] == ["agent.zavod.local", "10.0.0.15"]
    assert tls.ca_constrained() == ({"agent.zavod.local"}, {"10.0.0.15"})
    tls.remove()
    assert tls.paths() is None and tls.CA_CERT.exists()


def test_agent_ca_cannot_vouch_for_other_sites(tls_dir):
    """Даже с ключом центра агента сертификат «почты» или «банка» браузер не примет — только имена сервера."""
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
    tls.issue(["agent.zavod.local"], ["10.0.0.15"])
    ca = x509.load_pem_x509_certificate(tls.CA_CERT.read_bytes())
    ca_key = serialization.load_pem_private_key(tls.CA_KEY.read_bytes(), None)
    now = dt.datetime.now(dt.timezone.utc)

    def leaf(host: str):
        key = ec.generate_private_key(ec.SECP256R1())
        return (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))

    def verify(host: str):
        builder = PolicyBuilder().store(Store([ca])).time(now)
        return builder.build_server_verifier(x509.DNSName(host)).verify(leaf(host), [])

    assert verify("agent.zavod.local")
    with pytest.raises(VerificationError):
        verify("mail.zavod.local")
    with pytest.raises(VerificationError):
        verify("online.bank.ru")


def test_unconstrained_agent_ca_from_before_is_replaced(tls_dir):
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())                        # центр, выпущенный до 31.10 — без ограничения
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KBAgent CA old")])
    now = dt.datetime.now(dt.timezone.utc)
    old = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
           .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
           .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
    tls.CA_CERT.write_bytes(old.public_bytes(serialization.Encoding.PEM))
    tls.CA_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    assert tls.ca_constrained() is None
    tls.issue(["localhost"], ["127.0.0.1"])
    assert tls.ca_constrained() == ({"localhost"}, {"127.0.0.1"}) and not tls.info()["ca_unconstrained"]


def test_pfx_from_it_department(tls_dir):
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent.zavod.local")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("agent.zavod.local")]), critical=False)
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(b"agent", key, cert, None,
                                                serialization.BestAvailableEncryption(b"pfx-pass-1"))
    with pytest.raises(ValueError, match="неверный пароль"):
        tls.from_pfx(pfx, "не тот")
    tls.from_pfx(pfx, "pfx-pass-1")
    info = tls.info()
    assert info["names"] == ["agent.zavod.local"] and not info["own_ca"] and 363 <= info["days_left"] <= 365
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tls.CERT, tls.KEY)


def test_streamlit_flags(tls_dir):
    tls.issue(["localhost"], [])
    assert tls.streamlit_flags(["--server.port", "8502"]) == ["--server.sslCertFile", str(tls.CERT),
                                                               "--server.sslKeyFile", str(tls.KEY)]
    assert tls.streamlit_flags(["--server.sslCertFile", "corp.pem"]) == []

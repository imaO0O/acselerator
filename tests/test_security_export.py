"""Audit export and scoped network observation; no production logs are read."""
import csv
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from kbagent import auth, security_export as export
from scripts import check_connections as network


@pytest.fixture
def logs(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    root.mkdir()
    monkeypatch.setattr(export.settings, "log_dir", root)
    monkeypatch.setattr(auth, "session_user", lambda token: {
        "role": "Администратор", "login": "chief"} if token == "valid" else None)
    return root


def read_export(source="actions.jsonl"):
    data = export.export_csv(source, session_token="valid")
    assert data.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


@pytest.mark.parametrize("token", [None, "", "expired", "forged"])
def test_export_denies_before_path_access(logs, token):
    with pytest.raises(PermissionError):
        export.export_csv("../../secrets", session_token=token)


@pytest.mark.parametrize("user", [{"role": "Метролог"}, {"is_admin": True},
                                    {"role": "Администратор", "must_change": True}])
def test_export_requires_current_admin(logs, monkeypatch, user):
    monkeypatch.setattr(auth, "session_user", lambda _: user)
    with pytest.raises(PermissionError):
        export.export_csv(session_token="valid")


def test_export_real_session_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr(export.settings, "log_dir", tmp_path)
    user = {"login": "chief", "role": "Администратор", "name": "Chief"}
    monkeypatch.setattr(auth, "load_users", lambda: {"chief": user})
    (tmp_path / "actions.jsonl").write_text('{}\n', encoding="utf-8")
    token = auth.create_session(user)
    assert export.export_csv(session_token=token)
    user["active"] = False
    with pytest.raises(PermissionError):
        export.export_csv(session_token=token)


@pytest.mark.parametrize("source", ["../actions.jsonl", "logs/actions.jsonl", "sessions.json",
                                     "feedback.jsonl", "history/x.jsonl", "ACTIONS.JSONL", "", None])
def test_source_allowlist(logs, source):
    with pytest.raises(ValueError):
        export.export_csv(source, session_token="valid")


def test_absolute_source_denied(logs):
    with pytest.raises(ValueError):
        export.export_csv(str(logs / "actions.jsonl"), session_token="valid")


def test_symlink_escape_denied(logs, tmp_path):
    outside = tmp_path / "secret.jsonl"
    outside.write_text('{}', encoding="utf-8")
    try:
        (logs / "actions.jsonl").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit symlinks for this test account")
    with pytest.raises(ValueError):
        export.export_csv(session_token="valid")


@pytest.mark.parametrize("payload", ["=SUM(1,2)", "+cmd", "-1+2", "@x", " \t=1", "\ufeff=2",
                                      "\tplain", "\rplain", "\nplain"])
def test_formula_and_control_prefixes(logs, payload):
    record = {"user": payload, "details": 'Кириллица, "quote"\nnext', "target": "safe"}
    path = logs / "actions.jsonl"
    raw = json.dumps(record, ensure_ascii=False) + "\n"
    path.write_text(raw, encoding="utf-8")
    rows = read_export()
    assert rows[0]["user"] == "'" + payload
    assert rows[0]["details"] == record["details"]
    assert path.read_text(encoding="utf-8") == raw


def test_audit_nested_and_extra_fields(logs):
    record = {"question": "=bad", "cited": ["=citation", "Русский"], "unapproved": False,
              "new_field": {"x": "=new"}, "answer": None, "seconds": 1.5}
    (logs / "audit.jsonl").write_text(json.dumps(record) + "\n\n", encoding="utf-8")
    row = read_export("audit.jsonl")[0]
    assert row["question"] == "'=bad"
    assert json.loads(row["cited"]) == record["cited"]
    assert json.loads(row["extra"]) == {"new_field": record["new_field"]}
    assert row["answer"] == "" and row["unapproved"] == "false"


def test_unicode_line_separator_is_preserved(logs):
    (logs / "actions.jsonl").write_text(json.dumps({"details": "a\u2028b"}, ensure_ascii=False), encoding="utf-8")
    assert read_export()[0]["details"] == "a\u2028b"


@pytest.mark.parametrize("data", ['{}\n{', '[]', 'null', '{"x": NaN}', '{}\n{"unfinished":'])
def test_malformed_export_fails_without_partial_result(logs, data):
    (logs / "actions.jsonl").write_text(data, encoding="utf-8")
    with pytest.raises(ValueError, match="line"):
        read_export()


def test_empty_missing_and_bounded_export(logs, monkeypatch):
    with pytest.raises(FileNotFoundError):
        read_export()
    (logs / "actions.jsonl").write_text('', encoding="utf-8")
    assert read_export() == []
    (logs / "actions.jsonl").write_text('{}\n{}\n', encoding="utf-8")
    monkeypatch.setattr(export, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="size limit"):
        read_export()


@pytest.mark.parametrize("host,expected", [("127.0.0.1", "loopback"), ("::1", "loopback"),
    ("::ffff:127.0.0.1", "loopback"), ("10.2.3.4", "private"), ("172.31.0.1", "private"),
    ("192.168.0.3", "private"), ("fd01::1", "private"), ("8.8.8.8", "external"),
    ("2606:4700:4700::1111", "external"), ("0.0.0.0", "unspecified"), ("*", "unspecified"),
    ("fe80::1%4", "link_local"), ("224.0.0.1", "multicast"), ("hostname", "unknown")])
def test_endpoint_classification(host, expected):
    assert network.classify(host) == expected


def test_windows_parser_filters_unrelated_pids_and_handles_ipv6_udp():
    rows = network.parse_netstat('''
  Active Connections
  TCP    127.0.0.1:1000    127.0.0.1:8080    ESTABLISHED    12
  TCP    [::]:8081         [::]:0            LISTENING      12
  UDP    0.0.0.0:5353      *:*                              12
  TCP    10.0.0.2:9999     8.8.8.8:443       ESTABLISHED    99
''', {12})
    assert len(rows) == 3 and {r["pid"] for r in rows} == {12}
    assert rows[0]["remote"]["classification"] == "loopback"
    assert rows[1]["local"]["port"] == 8081
    assert rows[2]["remote"]["port"] is None


def test_pid_selection_never_includes_siblings():
    parents = {1: 0, 10: 1, 11: 10, 12: 11, 20: 1}
    assert network.select_pids([10], parents, True) == {10, 11, 12}
    assert network.select_pids([10], parents, False) == {10}
    with pytest.raises(ValueError):
        network.select_pids([404], parents, True)


def test_proc_address_decoding():
    # Fixtures encode kernel-native 32-bit words.
    import struct
    def encode(host):
        packed = socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return b"".join(struct.pack("=I", n) for n in struct.unpack("!" + "I" * (len(packed)//4), packed)).hex()
    for host in ("127.0.0.1", "::1", "2606:4700:4700::1111"):
        row = network.proc_endpoint(encode(host) + ":1F90")
        assert row["address"] == host and row["port"] == 8080


def test_linux_filters_namespace_tables_by_owned_inode(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    base = proc / "12"
    (base / "fd").mkdir(parents=True)
    (base / "net").mkdir()
    monkeypatch.setattr(network, "Path", lambda _: proc)
    monkeypatch.setattr(network.os, "readlink", lambda _: "socket:[123]")
    (base / "fd" / "4").write_text("")
    header = "header\n"
    owned = "0: 0100007F:1F90 0100007F:C350 01 0:0 0:0 0 1000 0 123\n"
    unrelated = "1: 0100007F:1F90 08080808:01BB 01 0:0 0:0 0 1000 0 999\n"
    (base / "net" / "tcp").write_text(header + owned + unrelated)
    (base / "net" / "udp").write_text(header)
    errors = []
    rows = network.linux_connections({12}, errors)
    assert errors == [] and len(rows) == 1
    assert rows[0]["remote"]["classification"] == "loopback"
    errors = []
    assert network.linux_connections({404}, errors) == []
    assert errors


def test_snapshot_errors_are_json_and_never_leak_system_output(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(network, "process_parents", lambda: {12: 1})
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "netstat", output="SECRET unrelated rows")
    monkeypatch.setattr(network.subprocess, "run", fail)
    report = network.snapshot([12])
    assert not report["complete"] and report["errors"]
    assert "SECRET" not in json.dumps(report)
    assert "Snapshot only" in report["limitations"][0]


def test_windows_snapshot_success_and_cli(monkeypatch, capsys):
    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(network, "process_parents", lambda: {12: 1, 13: 12, 99: 1})
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "TCP 127.0.0.1:1 8.8.8.8:443 ESTABLISHED 13\nTCP 127.0.0.1:2 1.1.1.1:443 ESTABLISHED 99")
    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.main(["--pid", "12", "--include-children"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected_pids"] == [12, 13]
    assert len(report["connections"]) == 1 and report["external_remote_count"] == 1
    assert calls[0][0] == ["netstat", "-ano", "-n"] and not calls[0][1].get("shell")
    assert network.main(["--pid", "-1"]) == 1
    assert not json.loads(capsys.readouterr().out)["complete"]


def test_linux_partial_visibility_is_not_complete(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "linux")
    monkeypatch.setattr(network, "process_parents", lambda: {12: 1})
    def partial(pids, errors):
        errors.append("permission denied")
        return []
    monkeypatch.setattr(network, "linux_connections", partial)
    assert network.snapshot([12])["complete"] is False


def test_live_selected_process_snapshot():
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        pytest.skip("Supported platforms only")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        report = network.snapshot([os.getpid()])
        if not report["complete"]:
            # Sandboxed Windows may return headers only even with a live listener.
            assert sys.platform == "win32"
            assert any("visibility cannot be verified" in error for error in report["errors"])
            return
        assert {r["pid"] for r in report["connections"]} <= {os.getpid()}
        assert any(r["local"]["port"] == listener.getsockname()[1]
                   and r["local"]["classification"] == "loopback" for r in report["connections"])

"""Offline bundle verification; fixtures reproduce make_bundle's exact format."""
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import verify_bundle as verifier


def manifest(root, entries=None):
    if entries is None:
        entries = [(hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(root).as_posix())
                   for path in sorted(root.rglob("*")) if path.is_file() and path.name != verifier.MANIFEST]
    (root / verifier.MANIFEST).write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "copied bundle"
    (root / "agent" / "models").mkdir(parents=True)
    (root / "agent" / "models" / "model.gguf").write_bytes(b"offline model\x00" * 200)
    (root / "agent" / "Русский файл.txt").write_text("Скопировано без интернета", encoding="utf-8")
    (root / "install.bat").write_bytes(b"@echo off\r\n")
    manifest(root)
    return root


def test_valid_bundle_without_network_or_subprocess(bundle, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Verifier must not access network or execute commands")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    report = verifier.verify_bundle(bundle)
    assert report["ok"] is True
    assert report["verified_files"] == report["declared_files"] == 3
    assert report["bytes_hashed"] == sum(len(data) for path, data in before.items() if path.name != verifier.MANIFEST)
    assert report["missing"] == report["unexpected"] == report["mismatched"] == report["errors"] == []
    assert report["authenticity"]["verified"] is False
    assert "replace both" in report["authenticity"]["explanation"]
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    json.dumps(report)


def test_tamper_hashes_remaining_files_too(bundle):
    (bundle / "install.bat").write_bytes(b"tampered")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"] and report["verified_files"] == 2
    mismatch, = report["mismatched"]
    assert mismatch["path"] == "install.bat"
    assert mismatch["actual"] == hashlib.sha256(b"tampered").hexdigest()
    assert mismatch["expected"] != mismatch["actual"]


def test_missing_and_extra_are_both_reported(bundle):
    (bundle / "install.bat").unlink()
    (bundle / "unlisted.exe").write_bytes(b"unexpected")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert report["missing"] == ["install.bat"]
    assert report["unexpected"] == ["unlisted.exe"]


def test_only_root_manifest_is_excluded(bundle):
    (bundle / "agent" / verifier.MANIFEST).write_text("unlisted nested manifest")
    (bundle / ".hidden").write_text("unlisted hidden file")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert report["unexpected"] == [".hidden", "agent/SHA256SUMS.txt"]


@pytest.mark.parametrize("name", ["../escape", "agent/../../escape", "/absolute", "C:/absolute",
    "C:relative", "\\\\server\\share\\secret", "//server/share/secret", "agent\\..\\escape",
    "./install.bat", "agent//file", "agent/./file", "agent/../install.bat", "agent/file:stream",
    "agent/trailing.", "agent/trailing ", "agent/NUL", "agent/CON.txt", "agent/COM1",
    "agent/evil\x00name", "agent/evil\tname", "SHA256SUMS.txt"])
def test_unsafe_manifest_paths_are_never_hashed(bundle, monkeypatch, name):
    with (bundle / verifier.MANIFEST).open("a", encoding="utf-8") as stream:
        stream.write("0" * 64 + "  " + name + "\n")
    monkeypatch.setattr(verifier, "_hash_file", lambda *args: pytest.fail("Unsafe manifest must not be hashed"))
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(error["kind"] == "unsafe_path" for error in report["errors"])
    assert report["verified_files"] == 0


@pytest.mark.parametrize("line", ["", "not a digest  install.bat", "g" * 64 + "  install.bat",
    "0" * 63 + "  install.bat", "0" * 65 + "  install.bat", "0" * 64 + " install.bat",
    "0" * 64 + " *install.bat", "0" * 64 + "  ", "# comment"])
def test_malformed_digest_or_record_rejected(bundle, line):
    with (bundle / verifier.MANIFEST).open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "malformed_manifest" for e in report["errors"])


@pytest.mark.parametrize("name", ["install.bat", "INSTALL.BAT"])
def test_duplicate_and_case_alias_rejected(bundle, name):
    with (bundle / verifier.MANIFEST).open("a", encoding="utf-8") as stream:
        stream.write("0" * 64 + "  " + name + "\n")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "duplicate_entry" for e in report["errors"])


def test_empty_missing_invalid_utf8_and_overlong_manifest(bundle, monkeypatch):
    path = bundle / verifier.MANIFEST
    for raw in (b"", b"\xff"):
        path.write_bytes(raw)
        assert not verifier.verify_bundle(bundle)["ok"]
    path.unlink()
    report = verifier.verify_bundle(bundle)
    assert any(e["kind"] == "manifest_error" for e in report["errors"])
    path.write_bytes(b"x" * 20)
    monkeypatch.setattr(verifier, "MAX_LINE", 10)
    assert not verifier.verify_bundle(bundle)["ok"]


def test_crlf_uppercase_digest_and_final_line_without_newline(bundle):
    path = bundle / verifier.MANIFEST
    entries = path.read_text(encoding="utf-8").splitlines()
    entries = [line[:64].upper() + line[64:] for line in entries]
    path.write_bytes("\r\n".join(entries).encode("utf-8"))
    assert verifier.verify_bundle(bundle)["ok"]


def _symlink(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Account cannot create symlinks on this OS")


@pytest.mark.parametrize("kind", ["listed_file", "unlisted_file", "directory", "manifest", "broken"])
def test_symlinks_rejected_without_following(bundle, tmp_path, monkeypatch, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_bytes(b"must not read")
    if kind == "listed_file":
        link = bundle / "install.bat"
        link.unlink()
    elif kind == "manifest":
        link = bundle / verifier.MANIFEST
        link.unlink()
    elif kind == "directory":
        link, target = bundle / "linked-dir", outside
    elif kind == "broken":
        link, target = bundle / "broken", outside / "absent"
    else:
        link = bundle / "linked"
    _symlink(link, target, kind == "directory")
    monkeypatch.setattr(verifier, "_hash_file", lambda *args: pytest.fail("Links must fail before hashing"))
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "unsafe_file" for e in report["errors"])


def test_root_symlink_rejected(bundle, tmp_path):
    link = tmp_path / "alias"
    _symlink(link, bundle, True)
    assert not verifier.verify_bundle(link)["ok"]


def test_windows_reparse_points_are_rejected_even_without_symlink_privilege():
    info = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
    assert verifier._linked(info)


@pytest.mark.parametrize("name", ["install.bat", "agent", "SHA256SUMS.txt"])
@pytest.mark.parametrize("kind", ["symlink", "reparse"])
def test_link_rejection_without_os_symlink_privilege(bundle, monkeypatch, name, kind):
    target = bundle / name
    original = Path.lstat
    def linked(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else stat.S_IFREG,
                                   st_file_attributes=0 if kind == "symlink" else 0x400)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", linked)
    monkeypatch.setattr(verifier, "_hash_file", lambda *args: pytest.fail("Rejected tree must not be hashed"))
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "unsafe_file" and e["path"] == name for e in report["errors"])


def test_file_replaced_between_inventory_and_open_is_rejected(bundle, monkeypatch):
    original = verifier._hash_file
    def replace_before_open(path, expected):
        if path.name == "install.bat":
            path.write_bytes(b"replaced after inventory")
        return original(path, expected)
    monkeypatch.setattr(verifier, "_hash_file", replace_before_open)
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "file_error" and "changed since inventory" in e["message"] for e in report["errors"])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="OS has no FIFO creation")
def test_special_file_is_rejected_without_blocking(bundle):
    os.mkfifo(bundle / "pipe")
    report = verifier.verify_bundle(bundle)
    assert not report["ok"] and any(e["kind"] == "unsafe_file" for e in report["errors"])


def test_streaming_large_file_never_uses_unbounded_reads(bundle, monkeypatch):
    path = bundle / "agent" / "models" / "model.gguf"
    path.write_bytes(b"x" * (verifier.CHUNK_SIZE * 2 + 17))
    manifest(bundle)
    original = verifier._open_regular
    reads = []
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            assert 0 < size <= verifier.CHUNK_SIZE
            reads.append(size)
            return self.stream.read(size)
    def wrapped(candidate, expected=None):
        stream, info = original(candidate, expected)
        return (BoundedReader(stream), info) if candidate == path else (stream, info)
    monkeypatch.setattr(verifier, "_open_regular", wrapped)
    assert verifier.verify_bundle(bundle)["ok"]
    assert len(reads) == 4


def test_file_read_error_fails_closed(bundle, monkeypatch):
    original = verifier._hash_file
    def denied(path, expected):
        if path.name == "install.bat":
            raise PermissionError("access denied")
        return original(path, expected)
    monkeypatch.setattr(verifier, "_hash_file", denied)
    report = verifier.verify_bundle(bundle)
    assert not report["ok"] and report["verified_files"] == 2
    assert any(e["kind"] == "file_error" for e in report["errors"])


def test_bundle_mutation_during_verification_fails(bundle, monkeypatch):
    original = verifier._hash_file
    def mutate(path, expected):
        result = original(path, expected)
        (bundle / "appeared.exe").write_bytes(b"new")
        return result
    monkeypatch.setattr(verifier, "_hash_file", mutate)
    report = verifier.verify_bundle(bundle)
    assert not report["ok"]
    assert any(e["kind"] == "bundle_changed" for e in report["errors"])


def test_root_errors_are_json(tmp_path):
    for root in (tmp_path / "absent", tmp_path / "file"):
        if root.name == "file":
            root.write_text("not a directory")
        report = verifier.verify_bundle(root)
        assert not report["ok"] and report["errors"][0]["kind"] == "root_error"
        json.dumps(report)


def test_unc_rejected_before_io(monkeypatch):
    monkeypatch.setattr(verifier, "_plain_path", lambda _: pytest.fail("Must reject network path before filesystem I/O"))
    for root in ("\\\\server\\share", "//server/share", "\\\\?\\C:\\bundle"):
        assert not verifier.verify_bundle(root)["ok"]


def test_cli_json_and_external_output(bundle, tmp_path, capsys):
    output = tmp_path / "report.json"
    assert verifier.main(["--root", str(bundle), "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and json.loads(output.read_text()) == report
    assert verifier.verify_bundle(bundle)["ok"]  # Report did not become an extra bundle file.
    assert verifier.main(["--root", str(bundle), "--output", str(output)]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]  # Do not clobber existing reports.


@pytest.mark.parametrize("output", ["report.json", "agent/report.json", "SHA256SUMS.txt"])
def test_cli_output_inside_bundle_rejected_without_writes(bundle, capsys, output):
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    assert verifier.main(["--root", str(bundle), "--output", str(bundle / output)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(e["kind"] == "output_error" for e in report["errors"])
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


def test_cli_tamper_returns_nonzero_json(bundle, capsys):
    (bundle / "install.bat").write_bytes(b"changed")
    assert verifier.main(["--root", str(bundle)]) == 1
    assert json.loads(capsys.readouterr().out)["mismatched"]


def test_script_entrypoint_from_outside_bundle(bundle, tmp_path):
    script = Path(verifier.__file__).resolve()
    for tampered in (False, True):
        if tampered:
            (bundle / "install.bat").write_bytes(b"modified")
        result = subprocess.run([sys.executable, str(script), "--root", str(bundle)],
                                cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert result.returncode == (1 if tampered else 0), result.stderr
        assert json.loads(result.stdout)["ok"] is (not tampered)


def test_cli_output_symlink_parent_rejected(bundle, tmp_path, capsys):
    alias = tmp_path / "alias"
    _symlink(alias, bundle, True)
    assert verifier.main(["--root", str(bundle), "--output", str(alias / "report.json")]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not (bundle / "report.json").exists()

"""Index encryption tests use only temporary indexes and externally supplied keys."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from kbagent import index_crypto as crypto, store
from kbagent.ingest import Chunk
from kbagent.store import Index, disk_stamp
from scripts.encrypt_index import migrate


@pytest.fixture(autouse=True)
def no_external_key(monkeypatch):
    monkeypatch.delenv("KB_INDEX_KEY_FILE", raising=False)


@pytest.fixture
def index():
    chunks = [Chunk("secret-id", "secret-doc", "Secret title", "private", "restricted",
                    "1.2", "Secret heading", 5, 6, "Секретное содержимое корпуса")]
    vectors = np.array([[0.1234567, -0.7654321, 0.9876543, 0.4567891]], dtype=np.float32)
    return Index(chunks, vectors)


@pytest.fixture
def encrypted(tmp_path, monkeypatch):
    key = tmp_path / "external.key"
    key.write_bytes(os.urandom(32))
    monkeypatch.setenv("KB_INDEX_KEY_FILE", str(key))
    return tmp_path / "index", key


def assert_same(expected, actual):
    assert [c.to_dict() for c in actual.chunks] == [c.to_dict() for c in expected.chunks]
    np.testing.assert_array_equal(actual.vectors, expected.vectors)
    assert actual.vectors.dtype == expected.vectors.dtype


def test_default_plaintext_compatible_and_configuration_lazy(tmp_path, index, monkeypatch):
    plain = tmp_path / "plain"
    index.save(plain)
    assert_same(index, Index.load(plain))
    assert (plain / "chunks.jsonl").exists() and (plain / "vectors.npy").exists()
    assert crypto.status(plain)["enabled"] is False
    key = tmp_path / "external.key"
    key.write_bytes(os.urandom(32))
    monkeypatch.setenv("KB_INDEX_KEY_FILE", str(key))
    assert crypto.status(plain)["enabled"] is True
    with pytest.raises(crypto.IndexCryptoError):
        Index.load(plain)
    with pytest.raises(crypto.IndexCryptoError, match="explicit"):
        index.save(plain)
    assert (plain / "chunks.jsonl").exists()


def test_roundtrip_one_blob_no_plaintext_including_temporary_writes(encrypted, index, monkeypatch):
    directory, key = encrypted
    writes = []
    original_replace = os.replace

    def inspect_replace(src, dst):
        writes.append((Path(dst).name, Path(src).read_bytes()))
        assert (directory / ".lock").exists()
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", inspect_replace)
    index.save(directory)
    assert_same(index, Index.load(directory))
    assert {p.name for p in directory.iterdir()} == {"index.enc", "meta.json"}
    assert {name for name, _ in writes} == {"index.enc", "meta.json"}
    for _, data in writes:
        for secret in (index.chunks[0].text.encode(), b"Secret title", b"secret-doc",
                       index.vectors.tobytes(), key.read_bytes(), b"NUMPY"):
            assert secret not in data
    meta = json.loads((directory / "meta.json").read_text())
    assert set(meta) == {"chunks", "built", "encryption"}
    assert meta["encryption"] is True and meta["chunks"] == 1
    assert crypto.status(directory) == {"enabled": True, "encryption": True,
                                        "key_configured": True, "algorithm": "AES-256-GCM"}
    assert disk_stamp(directory) == index.stamp


@pytest.mark.parametrize("failure", ["unset", "missing", "wrong", "short", "inside"])
def test_key_failures_load_reload_and_save_fail_closed(encrypted, index, monkeypatch, failure):
    directory, key = encrypted
    index.save(directory)
    before = (directory / "index.enc").read_bytes()
    if failure == "unset":
        monkeypatch.delenv("KB_INDEX_KEY_FILE")
    elif failure == "missing":
        key.unlink()
    elif failure == "wrong":
        key.write_bytes(os.urandom(32))
    elif failure == "short":
        key.write_bytes(b"password")
    else:
        internal = directory / "key"
        internal.write_bytes(key.read_bytes())
        monkeypatch.setenv("KB_INDEX_KEY_FILE", str(internal))
    for action in (lambda: Index.load(directory), lambda: index.reload_if_changed(directory),
                   lambda: index.save(directory)):
        with pytest.raises(crypto.IndexCryptoError):
            action()
    assert (directory / "index.enc").read_bytes() == before
    assert not (directory / "chunks.jsonl").exists()
    assert not (directory / ".lock").exists()


@pytest.mark.parametrize("offset", [0, len(crypto.MAGIC), len(crypto.MAGIC) + 12 + 8, -17, -1])
def test_tamper_header_nonce_chunks_vectors_and_tag_rejected(encrypted, index, offset):
    directory, _ = encrypted
    index.save(directory)
    blob = bytearray((directory / "index.enc").read_bytes())
    blob[offset] ^= 1
    (directory / "index.enc").write_bytes(blob)
    os.utime(directory / "index.enc", ns=(index.stamp + 1000000, index.stamp + 1000000))
    # Residual legacy files must never become a fallback.
    (directory / "chunks.jsonl").write_text("{}\n")
    np.save(directory / "vectors.npy", np.zeros((1, 4)))
    with pytest.raises(crypto.IndexCryptoError):
        Index.load(directory)
    with pytest.raises(crypto.IndexCryptoError):
        index.reload_if_changed(directory)
    with pytest.raises(crypto.IndexCryptoError):
        index.save(directory)


def test_missing_blob_with_marker_never_falls_back(encrypted, index, monkeypatch):
    directory, _ = encrypted
    index.save(directory)
    (directory / "index.enc").unlink()
    (directory / "chunks.jsonl").write_text("{}\n")
    with pytest.raises(crypto.IndexCryptoError):
        Index.load(directory)
    with pytest.raises(crypto.IndexCryptoError):
        index.save(directory)
    monkeypatch.delenv("KB_INDEX_KEY_FILE")
    assert crypto.status(directory)["enabled"] is True
    with pytest.raises(crypto.IndexCryptoError):
        index.reload_if_changed(directory)


def test_reload_and_updates_preserve_other_writer_changes(encrypted, index):
    directory, _ = encrypted
    index.save(directory)
    other = Index.load(directory)
    index.update_meta("secret-doc", directory, doc_title="Changed title")
    assert other.reload_if_changed(directory)
    assert other.chunks[0].doc_title == "Changed title"
    assert not other.reload_if_changed(directory)
    other.update_meta("secret-doc", directory, access="all")
    index.update_meta("secret-doc", directory, doc_kind="Updated kind")
    fresh = Index.load(directory)
    assert fresh.chunks[0].doc_title == "Changed title"
    assert fresh.chunks[0].access == "all"
    assert fresh.chunks[0].doc_kind == "Updated kind"
    assert index.remove_document("secret-doc", directory) == 1
    empty = Index.load(directory)
    assert len(empty.chunks) == 0 and empty.vectors.shape == (0, 4)


def test_blob_commit_reloads_before_metadata_publish(encrypted, index, monkeypatch):
    directory, _ = encrypted
    index.save(directory)
    other = Index.load(directory)
    old_meta = (directory / "meta.json").read_bytes()
    original_write = crypto.atomic_write

    def stop_meta(path, data):
        if path.name == "meta.json":
            raise OSError("simulated crash after encrypted publish")
        original_write(path, data)

    monkeypatch.setattr(crypto, "atomic_write", stop_meta)
    index.chunks[0].text = "Changed content"
    with pytest.raises(OSError):
        index.save(directory)
    assert (directory / "meta.json").read_bytes() == old_meta
    assert other.reload_if_changed(directory)
    assert other.chunks[0].text == "Changed content"


def test_migration_verifies_publish_then_removes_plaintext_and_stale_temps(tmp_path, index, monkeypatch):
    directory = tmp_path / "index"
    key = tmp_path / "external.key"
    key.write_bytes(os.urandom(32))
    index.save(directory)
    (directory / "chunks.jsonl.123.tmp").write_bytes(b"stale plaintext")
    (directory / "vectors.123.tmp.npy").write_bytes(b"stale vectors")
    old_replace = os.replace

    def verify_order(src, dst):
        if Path(dst).name == "index.enc":
            assert (directory / "chunks.jsonl").exists()
            assert (directory / "vectors.npy").exists()
            crypto.unseal(Path(src).read_bytes(), key.read_bytes())
        return old_replace(src, dst)

    monkeypatch.setattr(os, "replace", verify_order)
    migrate(directory, key)
    monkeypatch.setenv("KB_INDEX_KEY_FILE", str(key))
    assert_same(index, Index.load(directory))
    assert {p.name for p in directory.iterdir()} == {"index.enc", "meta.json"}
    monkeypatch.setattr(os, "replace", old_replace)
    migrate(directory, key)  # Resumable/idempotent migration.
    assert_same(index, Index.load(directory))


def test_migration_publish_failure_keeps_legacy_and_cleans_ciphertext_temp(tmp_path, index, monkeypatch):
    directory = tmp_path / "index"
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    index.save(directory)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    def fail_replace(src, dst):
        raise OSError("publish denied")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        migrate(directory, key)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert_same(index, Index.load(directory))


def test_cli_requires_explicit_paths():
    script = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_index.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "--index-dir" in result.stderr and "--key-file" in result.stderr


def test_new_save_obeys_existing_lock(encrypted, index, monkeypatch):
    directory, _ = encrypted
    directory.mkdir()
    (directory / ".lock").write_text(str(os.getpid()))
    monkeypatch.setattr(store, "LOCK_WAIT", 0)
    with pytest.raises(TimeoutError):
        index.save(directory)
    assert not (directory / "index.enc").exists()


def test_first_update_can_publish_new_encrypted_index(encrypted, index):
    directory, _ = encrypted
    assert index.update_meta("secret-doc", directory, access="all") == 1
    assert Index.load(directory).chunks[0].access == "all"


def test_repeated_save_uses_new_nonce(encrypted, index):
    directory, _ = encrypted
    index.save(directory)
    before = (directory / "index.enc").read_bytes()
    index.save(directory)
    after = (directory / "index.enc").read_bytes()
    assert before != after
    assert before[len(crypto.MAGIC):len(crypto.MAGIC) + 12] != after[len(crypto.MAGIC):len(crypto.MAGIC) + 12]
    assert_same(index, Index.load(directory))

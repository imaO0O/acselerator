"""Optional AES-256-GCM index storage; configuration is read at runtime.

KB_INDEX_KEY_FILE must name an externally managed file containing exactly 32
random bytes (not a password), outside index_dir. No keys are generated here.
Keep custody and separate protected backups of the key: losing it loses access.
This protects only the RAG index, not source documents, trade SQLite databases,
old plaintext copies or backups. File deletion is not secure erasure.
"""
import io
import json
import os
import struct
import tempfile
from pathlib import Path

import numpy as np

BLOB_NAME = "index.enc"
MAGIC = b"KBINDEX\x01"


class IndexCryptoError(RuntimeError):
    """Encryption failures must escape the store's recoverable reload errors."""


def status(index_dir: Path) -> dict:
    """Public, secret-free configuration for UI; configured does not mean valid."""
    index_dir = Path(index_dir)
    encrypted = (index_dir / BLOB_NAME).exists()
    try:
        encrypted |= json.loads((index_dir / "meta.json").read_text(encoding="utf-8")).get(
            "encryption", False) is True
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        raise IndexCryptoError("Cannot determine index encryption state") from exc
    configured = bool(os.environ.get("KB_INDEX_KEY_FILE"))
    return {"enabled": configured or encrypted, "encryption": encrypted,
            "key_configured": configured, "algorithm": "AES-256-GCM" if configured or encrypted else None}


def read_key(index_dir: Path, key_file: Path | None = None) -> bytes:
    configured = key_file if key_file is not None else os.environ.get("KB_INDEX_KEY_FILE")
    if not configured:
        raise IndexCryptoError("Encrypted index requires KB_INDEX_KEY_FILE")
    try:
        path = Path(configured).resolve(strict=True)
        if path.is_relative_to(Path(index_dir).resolve()):
            raise IndexCryptoError("Index key must be outside the index directory")
        key = path.read_bytes()
    except OSError as exc:
        raise IndexCryptoError("Cannot read the supplied index key file") from exc
    if len(key) != 32:
        raise IndexCryptoError("Index key file must contain exactly 32 random bytes")
    return key


def _aes(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise IndexCryptoError("Index encryption requires the cryptography package") from exc
    return AESGCM(key)


def seal(chunks: list[dict], vectors: np.ndarray, key: bytes) -> bytes:
    """Serialize only in memory, then authenticate chunks and vectors together."""
    if vectors.ndim != 2 or len(chunks) != len(vectors) or vectors.dtype.hasobject:
        raise IndexCryptoError("Invalid index vectors or chunk count")
    text = json.dumps(chunks, ensure_ascii=False).encode("utf-8")
    buf = io.BytesIO()
    np.save(buf, vectors, allow_pickle=False)
    payload = struct.pack(">Q", len(text)) + text + buf.getvalue()
    nonce = os.urandom(12)
    return MAGIC + nonce + _aes(key).encrypt(nonce, payload, MAGIC)


def unseal(blob: bytes, key: bytes) -> tuple[list[dict], np.ndarray]:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 12 + 16:
        raise IndexCryptoError("Invalid encrypted index header")
    aes = _aes(key)
    try:
        offset = len(MAGIC)
        payload = aes.decrypt(blob[offset:offset + 12], blob[offset + 12:], MAGIC)
        size = struct.unpack(">Q", payload[:8])[0]
        chunks = json.loads(payload[8:8 + size])
        vectors = np.load(io.BytesIO(payload[8 + size:]), allow_pickle=False)
        if not isinstance(chunks, list) or vectors.ndim != 2 or len(chunks) != len(vectors):
            raise ValueError("Invalid index payload")
        return chunks, vectors
    except Exception as exc:
        raise IndexCryptoError("Encrypted index authentication or payload validation failed") from exc


def atomic_write(path: Path, data: bytes) -> None:
    """Publish complete bytes; encrypted callers never put plaintext in a temp file."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

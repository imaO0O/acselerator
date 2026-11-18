"""Content-addressed originals for trade provenance; never execute source content."""
import hashlib
import os
import re
import tempfile
from pathlib import Path


def archive(root: Path, data: bytes) -> str:
    if len(data) > 100 * 1024 * 1024:
        raise ValueError('Source exceeds 100 MiB')
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(root) / 'sources'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (digest + '.bin')
    fd, name = tempfile.mkstemp(dir=folder, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return digest


def read(root: Path, digest: str) -> bytes | None:
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid source digest')
    folder = (Path(root) / 'sources').resolve()
    path = folder / (digest + '.bin')
    if not path.exists():
        return None
    if path.is_symlink() or path.resolve().parent != folder:
        raise ValueError('Invalid source path')
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('Source exceeds size limit')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Source checksum mismatch')
    return data

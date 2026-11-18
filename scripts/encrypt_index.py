"""Explicit in-place migration; never generates or stores a key.

Usage: python scripts/encrypt_index.py --index-dir PATH --key-file EXTERNAL_PATH
Key file: exactly 32 random bytes, externally supplied and protected.
Only the index is encrypted: source documents, trade SQLite and backups remain
separate responsibilities. Retain the key securely; old backups may be plaintext.
Deleting legacy files is not secure erasure on SSDs or versioned storage.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import index_crypto  # noqa: E402
from kbagent.ingest import Chunk  # noqa: E402
from kbagent.store import Index, _lock  # noqa: E402


def migrate(index_dir: Path, key_file: Path) -> None:
    if not index_dir.is_dir():
        raise index_crypto.IndexCryptoError("Migration requires an existing index directory")
    key = index_crypto.read_key(index_dir, key_file)
    with _lock(index_dir):
        blob = index_dir / index_crypto.BLOB_NAME
        if blob.exists():
            chunks, vectors = index_crypto.unseal(blob.read_bytes(), key)
            index = Index([Chunk(**c) for c in chunks], vectors)
        else:
            if index_crypto.status(index_dir)["encryption"]:
                raise index_crypto.IndexCryptoError("Encrypted index blob is missing")
            with (index_dir / "chunks.jsonl").open(encoding="utf-8") as stream:
                chunks = [Chunk(**json.loads(line)) for line in stream]
            index = Index(chunks, np.load(index_dir / "vectors.npy", allow_pickle=False))
        index._save_unlocked(index_dir, migration_key=key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    args = parser.parse_args()
    migrate(args.index_dir, args.key_file)
    print("Index encrypted. Source documents, trade SQLite and backups are not covered. Protect the external key.")


if __name__ == "__main__":
    main()

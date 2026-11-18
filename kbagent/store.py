"""Индекс фрагментов: полнотекстовый (BM25) и векторный (bge-m3).

Векторы хранятся в numpy и ищутся точным перебором: для корпуса предприятия
в тысячи и десятки тысяч фрагментов это занимает миллисекунды, не требует
отдельного сервера и не блокирует файлы при одновременной работе чата и
скрипта оценки. При росте до сотен тысяч фрагментов этот класс заменяется
на векторную СУБД (например, Qdrant) без изменения остального кода.

Индекс могут менять несколько процессов — чат (загрузка документа администратором) и обработка папки
документов (scripts/sync_folder.py). Поэтому изменение идёт под блокировкой index_dir/.lock: перечитать то,
что записал другой процесс, изменить, записать. Файлы пишутся во временные и подменяются целиком, meta.json —
последним; чат перед ответом перечитывает индекс, если meta.json изменился (reload_if_changed).
При KB_INDEX_KEY_FILE фрагменты и векторы публикуются одним зашифрованным index.enc;
для него reload_if_changed отслеживает сам blob, а meta.json содержит только публичные сведения.
"""
import hashlib
import json
import math
import os
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import index_crypto, llm
from .ingest import Chunk, parse_corpus, parse_document
from .text import word_tokens


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(t) for t in docs_tokens]
        self.len = np.array([len(t) for t in docs_tokens], dtype=np.float32)
        self.avg = float(self.len.mean()) if len(self.len) else 0.0
        df = Counter(term for t in docs_tokens for term in set(t))
        n = len(docs_tokens)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}
        self.postings: dict[str, list[int]] = {}
        for i, counts in enumerate(self.tf):
            for term in counts:
                self.postings.setdefault(term, []).append(i)

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tf), dtype=np.float32)
        for term in set(query_tokens):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i in self.postings[term]:
                f = self.tf[i][term]
                s[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return s


LOCK_WAIT, LOCK_STALE = 120, 600      # секунд: ждать чужую запись; блокировка старше — от упавшего процесса


@contextmanager
def _lock(index_dir: Path):
    """Межпроцессная блокировка записи индекса: файл .lock создаётся атомарно (O_EXCL)."""
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / ".lock"
    deadline = time.time() + LOCK_WAIT
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"индекс занят другим процессом дольше {LOCK_WAIT} с ({path})")
            time.sleep(0.2)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def disk_stamp(index_dir: Path) -> int | None:
    """Encrypted blob is the commit point; legacy indexes use meta.json."""
    if index_crypto.status(index_dir)["enabled"]:
        try:
            return (index_dir / index_crypto.BLOB_NAME).stat().st_mtime_ns
        except FileNotFoundError:
            return None
    try:
        return (index_dir / "meta.json").stat().st_mtime_ns
    except FileNotFoundError:
        return None


class Index:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray):
        self.chunks = chunks
        self.vectors = vectors
        self.bm25 = BM25([word_tokens(c.search_text) for c in chunks])
        self.access = np.array([c.access for c in chunks])
        self.stamp: int | None = None          # версия индекса на диске, с которой совпадает эта копия
        self._crypto_key_tag: bytes | None = None

    # ---------- построение и загрузка ----------

    @classmethod
    def build(cls, raw_dir: Path, index_dir: Path, log=print) -> "Index":
        t0 = time.time()
        chunks = parse_corpus(raw_dir)
        log(f"Фрагментов: {len(chunks)} из {len({c.doc_id for c in chunks})} документов")
        vectors = llm.embed([c.search_text for c in chunks])
        log(f"Векторы: {vectors.shape} за {time.time() - t0:.0f} с")
        index = cls(chunks, vectors)
        index.save(index_dir)
        return index

    def save(self, index_dir: Path) -> None:
        """Файлы пишутся во временные и подменяются целиком: читающий процесс не увидит половину записи."""
        with _lock(index_dir):
            self._save_unlocked(index_dir)

    def _save_unlocked(self, index_dir: Path, *, migration_key: bytes | None = None) -> None:
        """Caller holds _lock; migration_key is only for explicit plaintext migration."""
        index_dir.mkdir(parents=True, exist_ok=True)
        if index_crypto.status(index_dir)["enabled"] or self._crypto_key_tag is not None or migration_key is not None:
            key = migration_key if migration_key is not None else index_crypto.read_key(index_dir)
            blob_path = index_dir / index_crypto.BLOB_NAME
            if blob_path.exists():
                # Refuse overwriting an existing encrypted index with a wrong key.
                index_crypto.unseal(blob_path.read_bytes(), key)
            elif migration_key is None:
                if index_crypto.status(index_dir)["encryption"] or self._crypto_key_tag is not None:
                    raise index_crypto.IndexCryptoError("Encrypted index blob is missing")
                if any((index_dir / name).exists() for name in ("chunks.jsonl", "vectors.npy")):
                    raise index_crypto.IndexCryptoError("Plaintext index requires explicit scripts/encrypt_index.py migration")
            blob = index_crypto.seal([c.to_dict() for c in self.chunks], self.vectors, key)
            index_crypto.unseal(blob, key)   # Verify before publishing or deleting legacy files.
            index_crypto.atomic_write(blob_path, blob)
            index_crypto.unseal(blob_path.read_bytes(), key)
            meta = {"chunks": len(self.chunks), "built": time.strftime("%Y-%m-%d %H:%M"), "encryption": True}
            index_crypto.atomic_write(index_dir / "meta.json", json.dumps(meta).encode("utf-8"))
            for name in ("chunks.jsonl", "vectors.npy"):
                (index_dir / name).unlink(missing_ok=True)
            # Stale temporary files from earlier plaintext saves also contain index data.
            for pattern in ("chunks.jsonl.*.tmp", "vectors.*.tmp.npy", "meta.json.*.tmp"):
                for path in index_dir.glob(pattern):
                    path.unlink()
            self._crypto_key_tag = hashlib.sha256(key).digest()
            self.stamp = disk_stamp(index_dir)
            return
        tmp = index_dir / f"chunks.jsonl.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, index_dir / "chunks.jsonl")
        tmp = index_dir / f"vectors.{os.getpid()}.tmp.npy"
        np.save(tmp, self.vectors)
        os.replace(tmp, index_dir / "vectors.npy")
        meta = {"chunks": len(self.chunks), "documents": len({c.doc_id for c in self.chunks}),
                "built": time.strftime("%Y-%m-%d %H:%M"), "embedding_model": "bge-m3"}
        tmp = index_dir / f"meta.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, index_dir / "meta.json")
        self.stamp = disk_stamp(index_dir)

    def _rebuild_word_index(self) -> None:
        # новые структуры строятся целиком и подставляются вместе — поиск в соседнем потоке не увидит смеси
        self.bm25, self.access = BM25([word_tokens(c.search_text) for c in self.chunks]), np.array(
            [c.access for c in self.chunks])

    def reload_if_changed(self, index_dir: Path) -> bool:
        """Индекс на диске изменил другой процесс (обработка папки, второй экземпляр чата) — перечитать."""
        stamp = disk_stamp(index_dir)
        encrypted = index_crypto.status(index_dir)["enabled"] or self._crypto_key_tag is not None
        if encrypted:
            key_tag = hashlib.sha256(index_crypto.read_key(index_dir)).digest()
            if (self.stamp is None and stamp is None and self._crypto_key_tag is None
                    and not index_crypto.status(index_dir)["encryption"]
                    and not any((index_dir / name).exists() for name in ("chunks.jsonl", "vectors.npy"))):
                return False                   # fresh index in memory, no disk index to reload yet
            if stamp is not None and stamp == self.stamp and key_tag == self._crypto_key_tag:
                return False
            fresh = Index.load(index_dir)    # Crypto errors must propagate, never serve stale plaintext.
        elif self.stamp is None or stamp is None or stamp == self.stamp:
            return False                       # индекс собран в памяти (тесты) или на диске не менялся
        else:
            try:
                fresh = Index.load(index_dir)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False                       # файлы недоступны или повреждены — в следующий раз
        if len(fresh.chunks) != len(fresh.vectors):
            return False
        self.chunks, self.vectors, self.bm25, self.access = fresh.chunks, fresh.vectors, fresh.bm25, fresh.access
        self.stamp = fresh.stamp
        self._crypto_key_tag = fresh._crypto_key_tag
        return True

    @contextmanager
    def _writing(self, index_dir: Path):
        """Изменение индекса: под блокировкой, поверх последней версии с диска, с записью в конце."""
        with _lock(index_dir):
            self.reload_if_changed(index_dir)
            yield
            self._save_unlocked(index_dir)

    def _drop(self, doc_id: str) -> int:
        keep = [i for i, c in enumerate(self.chunks) if c.doc_id != doc_id]
        removed = len(self.chunks) - len(keep)
        if removed:
            self.chunks, self.vectors = [self.chunks[i] for i in keep], self.vectors[keep]
            self._rebuild_word_index()
        return removed

    def add_document(self, path: Path, catalog: dict, index_dir: Path, progress=None,
                     stats: dict | None = None) -> list[Chunk]:
        """Добавляет (или заменяет) один документ без пересборки всего индекса.
        progress(готово, всего) — для полосы прогресса; stats — счётчики распознанных сканов."""
        new = parse_document(path, catalog, stats)
        # разбор и векторы — до блокировки: это долго, а другим процессам индекс нужен
        vectors = llm.embed([c.search_text for c in new], progress=progress) if new else None
        with self._writing(index_dir):
            self._drop(path.stem)
            if new:
                self.chunks = self.chunks + new
                self.vectors = np.vstack([self.vectors, vectors]) if len(self.vectors) else vectors
                self._rebuild_word_index()
        return new

    def update_meta(self, doc_id: str, index_dir: Path, **fields) -> int:
        """Меняет сведения о документе во всех его фрагментах без пересчёта векторов:
        access (отменённая редакция → archive), doc_kind (утверждён), doc_title."""
        if not any(c.doc_id == doc_id and any(getattr(c, k) != v for k, v in fields.items()) for c in self.chunks):
            self.reload_if_changed(index_dir)          # может, уже изменил другой процесс — тогда и делать нечего
            if not any(c.doc_id == doc_id and any(getattr(c, k) != v for k, v in fields.items())
                       for c in self.chunks):
                return 0
        changed = 0
        with self._writing(index_dir):
            for c in self.chunks:
                if c.doc_id == doc_id and any(getattr(c, k) != v for k, v in fields.items()):
                    for k, v in fields.items():
                        setattr(c, k, v)
                    changed += 1
            self.access = np.array([c.access for c in self.chunks])
        return changed

    def doc_ids(self) -> set[str]:
        return {c.doc_id for c in self.chunks}

    def remove_document(self, doc_id: str, index_dir: Path, save: bool = True) -> int:
        if not save:
            return self._drop(doc_id)
        self.reload_if_changed(index_dir)
        if not any(c.doc_id == doc_id for c in self.chunks):
            return 0                                   # нечего удалять — индекс на диске не трогаем
        with self._writing(index_dir):
            return self._drop(doc_id)

    @classmethod
    def load(cls, index_dir: Path) -> "Index":
        stamp = disk_stamp(index_dir)
        if index_crypto.status(index_dir)["enabled"]:
            key = index_crypto.read_key(index_dir)
            try:
                chunks, vectors = index_crypto.unseal((index_dir / index_crypto.BLOB_NAME).read_bytes(), key)
                index = cls([Chunk(**c) for c in chunks], vectors)
            except (OSError, TypeError, ValueError) as exc:
                raise index_crypto.IndexCryptoError("Cannot load encrypted index; plaintext fallback is forbidden") from exc
            index._crypto_key_tag = hashlib.sha256(key).digest()
            index.stamp = stamp
            return index
        with open(index_dir / "chunks.jsonl", encoding="utf-8") as f:
            chunks = [Chunk(**json.loads(line)) for line in f]
        index = cls(chunks, np.load(index_dir / "vectors.npy"))
        index.stamp = stamp
        return index

    # ---------- поиск ----------

    def _mask(self, allowed: set[str], docs: set[str] | None = None) -> np.ndarray:
        mask = np.isin(self.access, list(allowed))
        if docs:                                   # вопрос называет документ — ищем только в нём
            mask &= np.isin(np.array([c.doc_id for c in self.chunks]), list(docs))
        return mask

    def search_words(self, query: str, allowed: set[str], k: int, docs: set[str] | None = None) -> list[tuple[int, float]]:
        s = self.bm25.scores(word_tokens(query))
        s[~self._mask(allowed, docs)] = 0
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top if s[i] > 0]

    def search_meaning(self, query_vector: np.ndarray, allowed: set[str], k: int,
                       docs: set[str] | None = None) -> list[tuple[int, float]]:
        s = self.vectors @ query_vector
        s[~self._mask(allowed, docs)] = -1
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top]

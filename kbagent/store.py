"""Индекс фрагментов: полнотекстовый (BM25) и векторный (bge-m3).

Векторы хранятся в numpy и ищутся точным перебором: для корпуса предприятия
в тысячи и десятки тысяч фрагментов это занимает миллисекунды, не требует
отдельного сервера и не блокирует файлы при одновременной работе чата и
скрипта оценки. При росте до сотен тысяч фрагментов этот класс заменяется
на векторную СУБД (например, Qdrant) без изменения остального кода.
"""
import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np

from . import llm
from .ingest import Chunk, parse_corpus
from .text import tokenize


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


class Index:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray):
        self.chunks = chunks
        self.vectors = vectors
        self.bm25 = BM25([tokenize(c.search_text) for c in chunks])
        self.access = np.array([c.access for c in chunks])

    # ---------- построение и загрузка ----------

    @classmethod
    def build(cls, raw_dir: Path, index_dir: Path, log=print) -> "Index":
        t0 = time.time()
        chunks = parse_corpus(raw_dir)
        log(f"Фрагментов: {len(chunks)} из {len({c.doc_id for c in chunks})} документов")
        vectors = llm.embed([c.search_text for c in chunks])
        log(f"Векторы: {vectors.shape} за {time.time() - t0:.0f} с")
        index_dir.mkdir(parents=True, exist_ok=True)
        with open(index_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        np.save(index_dir / "vectors.npy", vectors)
        meta = {"chunks": len(chunks), "documents": len({c.doc_id for c in chunks}),
                "built": time.strftime("%Y-%m-%d %H:%M"), "embedding_model": "bge-m3"}
        (index_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return cls(chunks, vectors)

    @classmethod
    def load(cls, index_dir: Path) -> "Index":
        with open(index_dir / "chunks.jsonl", encoding="utf-8") as f:
            chunks = [Chunk(**json.loads(line)) for line in f]
        return cls(chunks, np.load(index_dir / "vectors.npy"))

    # ---------- поиск ----------

    def _mask(self, allowed: set[str]) -> np.ndarray:
        return np.isin(self.access, list(allowed))

    def search_words(self, query: str, allowed: set[str], k: int) -> list[tuple[int, float]]:
        s = self.bm25.scores(tokenize(query))
        s[~self._mask(allowed)] = 0
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top if s[i] > 0]

    def search_meaning(self, query_vector: np.ndarray, allowed: set[str], k: int) -> list[tuple[int, float]]:
        s = self.vectors @ query_vector
        s[~self._mask(allowed)] = -1
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top]

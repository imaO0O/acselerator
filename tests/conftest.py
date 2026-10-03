"""Общие заготовки тестов. Тесты не обращаются к моделям — проверяется логика вокруг них."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent.ingest import Chunk  # noqa: E402
from kbagent.retriever import Hit  # noqa: E402


def make_hit(text: str, doc_id: str = "doc", clause: str | None = "1.1", score: float = 0.9) -> Hit:
    chunk = Chunk(id=f"{doc_id}#{clause}", doc_id=doc_id, doc_title=f"Документ {doc_id}", doc_kind="Регламент",
                  access="all", clause=clause, heading=None, page_start=1, page_end=1, text=text)
    return Hit(chunk, score, 0, 0)


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Журналы, сеансы и диалоги — во временной папке."""
    from kbagent.config import settings
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    return settings

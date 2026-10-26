"""Папка документов → база знаний; индекс, который меняют два процесса. Векторы — заглушка, модели не нужны."""
import hashlib
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
import yaml
from docx import Document

from kbagent import folder_sync, llm, store
from kbagent.store import Index

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_embed(monkeypatch):
    def embed(texts, batch=32, progress=None):
        v = np.array([[b / 255 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)
    monkeypatch.setattr(llm, "embed", embed)


def empty_index(index_dir: Path) -> Index:
    index = Index([], np.zeros((0, 8), dtype=np.float32))
    index.save(index_dir)
    return index


def regulation(path: Path, deadline: str) -> None:
    doc = Document()
    for line in ["СТП 12-2025 «Порядок подготовки ТКП»", "1 Общие положения",
                 f"1.1 Ответ клиенту на запрос ТКП направляется не позднее {deadline}.",
                 "1.2 ТКП утверждает начальник отдела продаж."]:
        doc.add_paragraph(line)
    doc.save(path)


def test_folder_documents_are_added_updated_and_removed(tmp_path, tmp_settings, fake_embed):
    src, mirror, index_dir = tmp_path / "share", tmp_path / "raw" / "folder", tmp_path / "index"
    src.mkdir()
    regulation(src / "Regl_TKP.docx", "2 рабочих дней")
    (src / "Tekhnika").mkdir()
    shutil.copy(ROOT / "data" / "raw" / "internal" / "RI-DP-04_sredstva_izmerenij.pdf", src / "Tekhnika" / "RI-04.pdf")
    (src / "~$Regl_TKP.docx").write_bytes(b"temp")                      # временный файл Word — мимо
    (src / "catalog.yaml").write_text(yaml.safe_dump({"documents": [
        {"id": "Regl_TKP", "title": "СТП 12-2025 «Порядок подготовки ТКП»", "version": "2025", "access": "all",
         "owner": "Начальник отдела продаж"}]}, allow_unicode=True), encoding="utf-8")
    index = empty_index(index_dir)

    rep = folder_sync.sync(index, src, index_dir, mirror)
    assert sorted(rep.added) == ["Regl_TKP.docx", "Tekhnika/RI-04.pdf"] and not rep.errors
    assert {c.doc_id for c in index.chunks} == {"Regl_TKP", "RI-04"}
    tkp_chunks = [c for c in index.chunks if c.doc_id == "Regl_TKP"]
    assert tkp_chunks[0].doc_title == "СТП 12-2025 «Порядок подготовки ТКП»"      # из реестра в папке
    assert any("2 рабочих дней" in c.text and c.clause.startswith("1.1") for c in tkp_chunks)
    assert (mirror / "Regl_TKP.docx").exists() and (mirror / "RI-04.pdf").exists()
    disk = Index.load(index_dir)
    assert len(disk.chunks) == len(index.chunks) == len(disk.vectors)            # записано на диск

    again = folder_sync.sync(index, src, index_dir, mirror)
    assert again.unchanged == 2 and not again.changed

    time.sleep(0.05)
    regulation(src / "Regl_TKP.docx", "3 рабочих дней")                         # новая редакция в той же папке
    os.utime(src / "Tekhnika" / "RI-04.pdf")                                      # «тронули», содержимое то же
    upd = folder_sync.sync(index, src, index_dir, mirror)
    assert upd.updated == ["Regl_TKP.docx"] and upd.unchanged == 1
    assert any("3 рабочих дней" in c.text for c in index.chunks) and not any("2 рабочих" in c.text for c in index.chunks)

    (src / "Tekhnika" / "RI-04.pdf").unlink()
    gone = folder_sync.sync(index, src, index_dir, mirror)
    assert gone.removed == ["Tekhnika/RI-04.pdf"] and {c.doc_id for c in index.chunks} == {"Regl_TKP"}
    assert not (mirror / "RI-04.pdf").exists()
    assert folder_sync.last_report(mirror)["removed"] == 1


def test_network_failure_does_not_wipe_the_knowledge_base(tmp_path, tmp_settings, fake_embed):
    src, mirror, index_dir = tmp_path / "share", tmp_path / "raw" / "folder", tmp_path / "index"
    src.mkdir()
    for k in range(6):
        regulation(src / f"R{k}.docx", f"{k + 1} рабочих дней")
    index = empty_index(index_dir)
    assert len(folder_sync.sync(index, src, index_dir, mirror).added) == 6

    offline = folder_sync.sync(index, tmp_path / "net-dostupa", index_dir, mirror)
    assert "недоступна" in offline.warning and len({c.doc_id for c in index.chunks}) == 6

    for k in range(5):
        (src / f"R{k}.docx").unlink()
    partial = folder_sync.sync(index, src, index_dir, mirror)
    assert "не удалены" in partial.warning and not partial.removed and len({c.doc_id for c in index.chunks}) == 6
    forced = folder_sync.sync(index, src, index_dir, mirror, max_removed_share=1.0)
    assert len(forced.removed) == 5 and {c.doc_id for c in index.chunks} == {"R5"}


def test_index_changed_by_another_process_is_reloaded_not_overwritten(tmp_path, fake_embed, monkeypatch):
    index_dir = tmp_path / "index"
    a = empty_index(index_dir)
    b = Index.load(index_dir)                       # второй процесс — например, чат
    regulation(tmp_path / "Regl_TKP.docx", "2 рабочих дней")
    a.add_document(tmp_path / "Regl_TKP.docx", {}, index_dir)
    assert b.reload_if_changed(index_dir) and {c.doc_id for c in b.chunks} == {"Regl_TKP"}
    assert not b.reload_if_changed(index_dir)       # без изменений — не перечитывается
    regulation(tmp_path / "Other.docx", "5 рабочих дней")
    b.add_document(tmp_path / "Other.docx", {}, index_dir)
    a.add_document(tmp_path / "Regl_TKP.docx", {}, index_dir)        # a устарел, но запись идёт поверх диска
    assert {c.doc_id for c in Index.load(index_dir).chunks} == {"Regl_TKP", "Other"}

    lock = index_dir / ".lock"
    lock.write_text("123")
    monkeypatch.setattr(store, "LOCK_WAIT", 0.3)
    with pytest.raises(TimeoutError):
        a.remove_document("Other", index_dir)
    old = time.time() - store.LOCK_STALE - 5
    os.utime(lock, (old, old))                      # блокировка упавшего процесса снимается
    assert a.remove_document("Other", index_dir) > 0 and not lock.exists()

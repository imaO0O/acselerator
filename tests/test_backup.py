"""Резервная копия данных заказчика и перенос в новую версию поверх демо-данных поставки."""
import socket
import zipfile
from pathlib import Path

import pytest

from kbagent import backup


def put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def customer_install(root: Path) -> None:
    put(root, "logs/audit.jsonl", '{"q": "срок ТКП"}\n')
    put(root, "logs/history/manager/c1.json", '{"items": []}')
    put(root, "data/users.yaml", "users: [manager]")
    put(root, "data/index/chunks.jsonl", '{"doc_id": "Regl_TKP"}\n')
    put(root, "data/index/meta.json", '{"chunks": 1}')
    put(root, "data/index/chunks.jsonl.123.tmp", "недописанный файл")
    put(root, "data/raw/uploaded/upl_Regl_TKP.pdf", "%PDF заказчика")
    put(root, "data/raw/folder/Prais.docx", "docx заказчика")
    put(root, "data/spravochnik/prajs.csv", "Модель;Цена\nDT9205A;1390\n")
    put(root, "data/raw/internal/demo.pdf", "демо из поставки — не копируется")


def test_backup_moves_customer_state_into_new_version(tmp_path):
    old, new = tmp_path / "v1", tmp_path / "v2"
    customer_install(old)
    archive = backup.create(old, tmp_path / "arch")
    names = set(zipfile.ZipFile(archive).namelist())
    assert "data/raw/uploaded/upl_Regl_TKP.pdf" in names and "logs/history/manager/c1.json" in names
    assert "data/raw/internal/demo.pdf" not in names and not any(n.endswith(".tmp") or n.endswith(".lock") for n in names)
    manifest = backup.verify(archive)
    assert "загружено через чат — 1" in backup.summary(manifest) and "из папки отдела — 1" in backup.summary(manifest)

    # новая версия: демо-индекс и демо-прайс из поставки, своих журналов нет
    put(new, "data/index/chunks.jsonl", '{"doc_id": "demo"}\n')
    put(new, "data/index/vectors.npy", "демо-векторы")
    put(new, "data/spravochnik/prajs.csv", "Модель;Цена\nDT9205A;1450\n")
    put(new, "data/raw/internal/demo.pdf", "демо новой версии")
    _, safety = backup.restore(archive, new)
    assert (new / "data/spravochnik/prajs.csv").read_text(encoding="utf-8").endswith("1390\n")
    assert (new / "data/index/chunks.jsonl").read_text(encoding="utf-8") == '{"doc_id": "Regl_TKP"}\n'
    assert not (new / "data/index/vectors.npy").exists()        # индекс — целиком из архива, без смешивания
    assert (new / "logs/audit.jsonl").exists() and (new / "data/raw/uploaded/upl_Regl_TKP.pdf").exists()
    assert (new / "data/raw/internal/demo.pdf").read_text(encoding="utf-8") == "демо новой версии"
    assert "data/spravochnik/prajs.csv" in zipfile.ZipFile(safety).namelist()   # страховка: что было до restore


def test_damaged_archive_is_not_restored(tmp_path):
    customer_install(tmp_path / "v1")
    archive = backup.create(tmp_path / "v1", tmp_path / "arch")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, b"%PDF \xd0\xb8\xd0\xb7\xd0\xbc\xd0\xb5\xd0\xbd\xd1\x91\xd0\xbd" if item.filename.endswith(".pdf")
                         else data)
    with pytest.raises(ValueError, match="контрольная сумма"):
        backup.restore(bad, tmp_path / "v2")
    assert not (tmp_path / "v2").exists()


def test_only_last_backups_are_kept_and_running_services_detected(tmp_path):
    customer_install(tmp_path / "v1")
    for _ in range(3):
        backup.create(tmp_path / "v1", tmp_path / "arch", keep=2)
    assert len(list((tmp_path / "arch").glob("agent_backup_*.zip"))) <= 2
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert backup.running({port: "чат"}) == [f"чат (порт {port})"]

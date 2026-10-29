"""Гриф документа и допуск сотрудника: документ с грифом виден, только если допуск не ниже."""
import numpy as np
import yaml

from kbagent import asof, changes, users
from kbagent.ingest import Chunk, access_of, allowed_groups, group_of, registry_suffix
from kbagent.store import Index


def test_groups_with_level():
    assert group_of({"access": "metrology", "level": 1}) == "metrology@1"
    assert group_of({"access": "all"}) == "all" and group_of({"access": "all", "level": "x"}) == "all"
    assert access_of({"access": "production", "level": 2}) == "production@2"
    assert access_of({"access": "all", "level": 2, "status": "отменён"}) == "archive"
    assert allowed_groups("Инженер производства") == {"all", "production"}
    assert allowed_groups("Инженер производства", 1) == {"all", "production", "all@1", "production@1"}
    assert "metrology@1" not in allowed_groups("Инженер производства", 2)      # гриф не расширяет роль


def test_search_respects_clearance():
    def chunk(i, access):
        return Chunk(id=f"d{i}#1", doc_id=f"d{i}", doc_title="Д", doc_kind="", access=access, clause="1",
                     heading=None, page_start=1, page_end=1, text="режим пайки печатных плат")
    index = Index([chunk(0, "all"), chunk(1, "all@1"), chunk(2, "all@2")], np.zeros((3, 4), dtype=np.float32))
    found = lambda clearance: {index.chunks[i].doc_id for i, _ in  # noqa: E731
                               index.search_words("режим пайки", allowed_groups("Инженер производства", clearance), 5)}
    assert found(0) == {"d0"} and found(1) == {"d0", "d1"} and found(2) == {"d0", "d1", "d2"}


def test_clearance_of_users(tmp_path, tmp_settings, monkeypatch):
    from kbagent.config import settings
    monkeypatch.setattr(settings, "users_path", tmp_path / "users.yaml")
    users.add("ivanov", "Иванов", "Инженер производства", "admin")
    users.add("petrova", "Петрова", "Метролог", "admin", clearance=1)
    users.first_run()                                         # администратор — со всеми грифами
    assert users.clearance("ivanov") == 0 and users.clearance("petrova") == 1 and users.clearance("admin") == 2
    assert users.clearance("eval") == 0 and users.clearance(None) == 0
    users.update("ivanov", "admin", clearance=2)
    assert users.clearance("ivanov") == 2
    assert {u["login"]: u["clearance"] for u in users.listing()} == {"ivanov": 2, "petrova": 1, "admin": 2}


def test_label_feed_and_asof_follow_level(tmp_path, tmp_settings):
    import datetime as dt
    raw = tmp_path / "raw"
    (raw / "internal").mkdir(parents=True)
    (raw / "internal" / "catalog.yaml").write_text(yaml.safe_dump(
        {"documents": [{"id": "dsp", "title": "РИ ДП-09", "level": 1, "version": "2026"}]}, allow_unicode=True),
        encoding="utf-8")
    assert registry_suffix("dsp", raw) == "гриф «для служебного пользования», ред. 2026"
    catalog = {"dsp": {"title": "РИ ДП-09 (ДСП)", "access": "all", "level": 1, "effective_from": "2024-01-01"}}
    w = asof.parse("на 01.06.2025", dt.date(2026, 10, 29))
    assert asof.scope(w, allowed_groups("Метролог"), {"dsp"}, catalog) == set()
    assert asof.scope(w, allowed_groups("Метролог", 1), {"dsp"}, catalog) == {"dsp"}
    changes.record("added", "dsp", "РИ ДП-09 (ДСП)", "all@1", "admin")
    now, since = "2099-01-01 00:00:00", "2000-01-01 00:00:00"
    assert changes.feed("belova", "Метролог", {}, now, since=since, clearance=0) == []
    assert [e["doc_id"] for e in changes.feed("belova", "Метролог", {}, now, since=since, clearance=1)] == ["dsp"]
    people = [{"login": "a", "name": "А", "role": "Метролог", "clearance": 0},
              {"login": "b", "name": "Б", "role": "Метролог", "clearance": 1}]
    assert [r["login"] for r in changes.sheet({"id": "x", "doc_id": "dsp", "access": "all@1", "by": "admin"},
                                              people)] == ["b"]


def test_api_key_clearance(tmp_path, monkeypatch, tmp_settings):
    from api import server
    monkeypatch.setattr(server, "KEYS_PATH", tmp_path / "keys.yaml")
    (tmp_path / "keys.yaml").write_text(yaml.safe_dump({"keys": [
        {"name": "бот", "role": "Метролог", "hash": server.key_hash("kb_1")},
        {"name": "1С", "role": "Метролог", "hash": server.key_hash("kb_2"), "clearance": 1}]}), encoding="utf-8")
    assert server.client("kb_1")["clearance"] == 0 and server.client("kb_2")["clearance"] == 1

"""Уведомления об изменениях документов: события, лента сотрудника по его роли, устаревшие ответы в диалогах."""
import time

from kbagent import changes, editions
from kbagent.editions import Clause

NOW = "2026-10-29 12:00:00"


def _cmp():
    old = [Clause("2.1", "2.1 При отказе работник сообщает диспетчеру по телефону 2200.", 1),
           Clause("3.1", "3.1 Мастер оформляет заявку.", 1)]
    new = [Clause("2.1", "2.1 При отказе работник сообщает диспетчеру по телефону 2211.", 1),
           Clause("4.1", "4.1 Отказы делятся на категории А, Б и В.", 2)]
    return editions.compare_clauses(old, new)


def test_summary_and_stored_changes(tmp_settings):
    cmp = _cmp()
    assert changes.summary(cmp) == "пунктов изменено: 1, добавлено: 1, исключено: 1"
    e = changes.record("updated", "stp02", "СТП ДП-02", "all", "papka", cmp=cmp)
    assert [c["kind"] for c in e["changes"]] == ["changed", "added", "removed"]
    assert "~~2200.~~ **2211.**" in e["changes"][0]["text"]
    assert changes.load()[-1]["id"] == e["id"]
    assert changes.summary(editions.compare_clauses([], [])) == "текст пунктов не изменился"


def test_feed_by_role_seen_and_own_uploads(tmp_settings, monkeypatch):
    monkeypatch.setattr(changes, "_now", lambda: "2026-10-28 09:00:00")
    changes.record("added", "ri04_new", "РИ ДП-04 (метрология)", "metrology", "admin")
    changes.record("edition", "stp02_2026", "СТП ДП-02-2026", "all", "admin", old_id="stp02_2024",
                   old_title="СТП ДП-02-2024", cmp=_cmp())
    changes.record("added", "own", "Мой документ", "all", "ivanov")
    feed = lambda login, role: [e["doc_id"] for e in changes.feed(login, role, {}, NOW)]  # noqa: E731
    assert feed("ivanov", "Инженер производства") == ["stp02_2026"]            # своё не показывается
    assert set(feed("belova", "Метролог")) == {"stp02_2026", "ri04_new", "own"}
    changes.mark_seen("belova", "2026-10-28 10:00:00")
    assert feed("belova", "Метролог") == []
    # пользователь без отметки видит изменения за две недели, не старше
    monkeypatch.setattr(changes, "_now", lambda: "2026-09-01 09:00:00")
    changes.record("added", "old", "Старый", "all", "admin")
    assert "old" not in feed("petrov", "Инженер производства")


def test_registry_dates_make_events_on_their_day(tmp_settings):
    catalog = {
        # утверждена заранее, вступила в силу 20.10 — сообщение в день вступления
        "stp02_new": {"title": "СТП ДП-02-2026", "replaces": "stp02", "approved": True, "approved_at": "2026-10-01 10:00",
                      "effective_from": "2026-10-20", "access": "all"},
        "stp02": {"title": "СТП ДП-02-2024", "status": "отменён", "replaced_by": "stp02_new", "access": "all"},
        "prikaz": {"title": "Приказ 903н", "effective_to": "2026-10-25", "access": "all"},
        "soon": {"title": "СТП ДП-09", "effective_from": "2026-12-01", "access": "all"},
        "demo": {"title": "СТП ДП-01", "effective_from": "2026-03-01", "access": "all"},
    }
    got = {e["doc_id"]: e for e in changes.feed("ivanov", "Инженер производства", catalog, NOW)}
    assert set(got) == {"stp02_new", "prikaz"}
    assert got["stp02_new"]["kind"] == "edition" and got["stp02_new"]["old_id"] == "stp02"
    assert got["stp02_new"]["time"] == "2026-10-20 00:00:00" and got["stp02_new"]["summary"] == "действует с 20.10.2026"
    assert got["prikaz"]["kind"] == "expired" and got["prikaz"]["time"] == "2026-10-26 00:00:00"


def test_stale_answers_in_dialogs(tmp_settings, monkeypatch):
    asked = time.mktime(time.strptime("2026-10-27 15:00:00", "%Y-%m-%d %H:%M:%S"))
    item = {"status": "answer", "ts": asked, "question": "Кому звонить при отказе?",
            "sources": [{"doc_id": "stp02_2024", "cited": True}, {"doc_id": "gost", "cited": False}]}
    assert changes.stale(item, {}) == []
    monkeypatch.setattr(changes, "_now", lambda: "2026-10-28 09:00:00")
    e = changes.record("edition", "stp02_2026", "СТП ДП-02-2026", "all", "admin", old_id="stp02_2024",
                       old_title="СТП ДП-02-2024")
    changes.record("updated", "gost", "ГОСТ", "all", "admin", note="файл заменён")      # не процитирован
    assert changes.stale(item, {}) == ["«СТП ДП-02-2024» заменён новой редакцией «СТП ДП-02-2026» (28.10.2026)"]
    assert changes.stale({**item, "as_of": "на 01.06.2025"}, {}) == []           # ответ на дату не устаревает
    later = {**item, "ts": asked + 86400 * 2}
    assert changes.stale(later, {}) == []                                       # спросили уже после изменения
    conv = {"items": [item, later, {**item, "sources": []}]}
    assert changes.affected_answers([conv], [e]) == {e["id"]: 1}


def test_upload_and_folder_record_changes(tmp_path, tmp_settings, monkeypatch):
    """Повторная загрузка того же файла — «обновлён» с итогом сравнения по прежним фрагментам индекса."""
    from kbagent import uploads
    from kbagent.ingest import Chunk

    class FakeIndex:
        def __init__(self):
            self.chunks = []

        def add_document(self, path, catalog, index_dir, progress=None, stats=None):
            text = path.read_bytes().decode("utf-8")
            new = [Chunk(id=f"{path.stem}#{i}", doc_id=path.stem, doc_title="Т", doc_kind="", access="all",
                         clause=f"{i}.1", heading=None, page_start=1, page_end=1, text=f"{i}.1 {line}")
                   for i, line in enumerate(text.splitlines(), 1)]
            self.chunks = [c for c in self.chunks if c.doc_id != path.stem] + new
            return new

    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploaded")
    monkeypatch.setattr(uploads, "CATALOG", tmp_path / "uploaded" / "catalog.yaml")
    monkeypatch.setattr(uploads, "convert_upload", lambda name, data: (".docx", data, {}))
    monkeypatch.setattr(uploads, "guess_title", lambda path: "РИ ДП-05 «Пайка»")
    monkeypatch.setattr(uploads, "load_catalog", lambda raw: {})
    index = FakeIndex()
    uploads.add_upload(index, "ri05.docx", "Температура жала 350 градусов.\nФлюс — ФКСп.".encode(), user="admin")
    uploads.add_upload(index, "ri05.docx", "Температура жала 320 градусов.\nФлюс — ФКСп.".encode(), user="admin")
    kinds = [(e["kind"], e.get("summary")) for e in changes.load()]
    assert kinds == [("added", None), ("updated", "пунктов изменено: 1")]
    assert "~~350~~ **320**" in changes.load()[-1]["changes"][0]["text"]

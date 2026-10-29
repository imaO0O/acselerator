"""Чек-лист внедрения и подключения: запись data/settings.env, применение без перезапуска, проверки."""
import os

import pytest

from kbagent import config, jobs, vnedrenie
from kbagent.config import settings


def test_write_settings_keeps_comments_and_replaces_examples(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("# Настройки\n# KB_DOCS_FOLDER=\\\\server\\пример\nKB_VAT_RATE=20\nKB_MAIL_HOST=old.ru\n"
                    "KB_MAIL_HOST=дубль.ru\n", encoding="utf-8")
    vnedrenie.write_settings({"KB_DOCS_FOLDER": "D:\\Документы", "KB_MAIL_HOST": "mail.zavod.ru",
                              "KB_1C_URL": "http://1c/base/odata/standard.odata", "KB_1C_USER": ""}, path)
    text = path.read_text(encoding="utf-8")
    assert "# Настройки" in text and "KB_VAT_RATE=20" in text and "дубль" not in text and "KB_1C_USER" not in text
    assert config.read_settings_file(path) == {"KB_DOCS_FOLDER": "D:\\Документы", "KB_VAT_RATE": "20",
                                               "KB_MAIL_HOST": "mail.zavod.ru",
                                               "KB_1C_URL": "http://1c/base/odata/standard.odata"}
    vnedrenie.write_settings({"KB_MAIL_HOST": ""}, path)
    assert "KB_MAIL_HOST" not in config.read_settings_file(path)


def test_apply_reaches_running_settings(monkeypatch):
    for attr in ("docs_folder", "docs_folder_access", "mail_port"):
        monkeypatch.setattr(settings, attr, getattr(settings, attr))
    for key in ("KB_DOCS_FOLDER", "KB_DOCS_FOLDER_ACCESS", "KB_MAIL_PORT", "KB_1C_URL"):
        monkeypatch.delenv(key, raising=False)
    vnedrenie.apply({"KB_DOCS_FOLDER": "D:\\Документы", "KB_DOCS_FOLDER_ACCESS": "production", "KB_MAIL_PORT": "143",
                     "KB_1C_URL": "http://1c/odata"})
    assert settings.docs_folder == "D:\\Документы" and settings.docs_folder_access == "production"
    assert settings.mail_port == 143 and os.environ["KB_1C_URL"] == "http://1c/odata"
    vnedrenie.apply({"KB_DOCS_FOLDER_ACCESS": "", "KB_MAIL_PORT": ""})
    assert settings.docs_folder_access == "all" and settings.mail_port == 993


def test_check_folder(tmp_path):
    (tmp_path / "СТП 1.pdf").write_bytes(b"%PDF")
    (tmp_path / "~$черновик.docx").write_bytes(b"x")
    assert vnedrenie.check_folder(str(tmp_path)) == (True, "папка доступна, документов (PDF, DOCX, Excel, сканы): 1")
    ok, message = vnedrenie.check_folder(str(tmp_path / "нет"))
    assert not ok and "недоступна" in message


def test_check_1c_finds_price_type(monkeypatch):
    from kbagent import onec
    monkeypatch.setenv("KB_1C_URL", "http://1c/base/odata/standard.odata")
    monkeypatch.setenv("KB_1C_USER", "agent")
    monkeypatch.setenv("KB_1C_PRICE_TYPE", "Оптовая")
    monkeypatch.setattr(onec, "_get", lambda c, path, params=None, session=None: [{"Description": "Розничная"},
                                                                                {"Description": "Оптовая "}])
    assert vnedrenie.check_1c()[0]
    monkeypatch.setenv("KB_1C_PRICE_TYPE", "Дилерская")
    ok, message = vnedrenie.check_1c()
    assert not ok and "Есть: Розничная, Оптовая" in message


@pytest.fixture
def fresh_install(tmp_path, tmp_settings, monkeypatch):
    """Установка «как из поставки»: демо-учётки, только демо-документы, нет действий, копий и прогонов."""
    from kbagent import onec, prajs, spravochnik, tkp_registry, tls, users
    monkeypatch.setattr(tls, "info", lambda: None)
    monkeypatch.setattr(spravochnik, "load", lambda: ([], None))
    monkeypatch.setattr(prajs, "load", lambda: ({}, None))
    monkeypatch.setattr(settings, "raw_dir", tmp_path / "raw")
    monkeypatch.setattr(settings, "docs_folder", "")
    monkeypatch.setattr(settings, "mail_host", "")
    monkeypatch.setattr(onec, "configured", lambda: False)
    monkeypatch.setattr(users, "demo_accounts", lambda: ["admin", "inzhener"])
    monkeypatch.setattr(users, "listing", lambda: [{"login": "admin", "active": True}])
    monkeypatch.setattr(tkp_registry, "load_settings", lambda: {"company": ""})
    monkeypatch.setattr(tkp_registry, "blank", lambda: None)
    monkeypatch.setattr(jobs, "newest_backup", lambda: None)
    monkeypatch.setattr(vnedrenie, "ROOT", tmp_path)
    return tmp_path


def test_fresh_install_checklist(fresh_install):
    items = {s.key: s for s in vnedrenie.steps(models_ok=True, registered={k: False for k in jobs.JOBS})}
    assert items["models"].done and not items["demo"].done and "admin, inzhener" in items["demo"].detail
    assert not items["documents"].done and not items["spravochnik"].done and "демонстрационный" in items["prajs"].detail
    assert items["mail"].done is None and items["folder"].done is None and items["1c"].done is None
    assert not items["jobs"].done and "Резервная копия" in items["jobs"].detail
    assert not items["backup"].done and not items["quality"].done
    done, total = vnedrenie.progress(list(items.values()))
    assert done == 1 and total == len(items) - 3


def test_checklist_sees_work_done(fresh_install, monkeypatch):
    from kbagent import tkp_registry, users
    from kbagent.actions import log_action
    monkeypatch.setattr(users, "demo_accounts", lambda: [])
    monkeypatch.setattr(users, "listing", lambda: [{"login": "ivanov", "active": True},
                                                   {"login": "petrova", "active": True}])
    monkeypatch.setattr(tkp_registry, "load_settings", lambda: {"company": "ООО «Завод»"})
    (fresh_install / "raw" / "uploaded").mkdir(parents=True)
    (fresh_install / "raw" / "uploaded" / "СТП 7.pdf").write_bytes(b"%PDF")
    log_action("spravochnik", "admin", "pribory.xlsx")
    log_action("prajs_1c", "1С", "Оптовая")
    (fresh_install / "eval" / "results").mkdir(parents=True)
    (fresh_install / "eval" / "results" / "partner.md").write_text("отчёт", encoding="utf-8")
    items = {s.key: s for s in vnedrenie.steps(registered={"backup": True})}
    assert "models" not in items
    for key in ("demo", "users", "documents", "spravochnik", "prajs", "tkp", "jobs", "quality"):
        assert items[key].done, key
    assert items["documents"].detail == "документов заказчика: 1"

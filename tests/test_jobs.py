"""Фоновые задачи по расписанию и общий файл настроек установки."""
import sys
import time

import pytest

from kbagent import config, jobs


@pytest.fixture
def fake_jobs(tmp_path, tmp_settings, monkeypatch):
    """Задачи — крошечные скрипты во временной папке, резервных копий нет, всё «настроено»."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "good.py").write_text("print('шаг 1')\nprint('готово: 3 письма')\n", encoding="utf-8")
    (scripts / "bad.py").write_text("import sys\nprint('не удалось подключиться')\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(jobs, "SCRIPTS", scripts)
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(jobs, "JOBS", {
        "backup": jobs.Job("backup", "Резервная копия", "good.py", (), 24 * 60, 2 * 24 * 60),
        "mail": jobs.Job("mail", "Почта отдела", "bad.py", (), 5, 30),
    })
    monkeypatch.setattr(jobs, "configured", lambda key: True)
    return tmp_path


def test_run_records_result_and_last_success(fake_jobs):
    rec = jobs.run("backup")
    assert rec["ok"] and rec["message"] == "готово: 3 письма" and "шаг 1" in rec["output"]
    assert jobs.last("backup")["last_ok"] == rec["started"]

    bad = jobs.run("mail")
    assert not bad["ok"] and bad["code"] == 1 and bad["message"] == "не удалось подключиться"
    rows = {r["key"]: r for r in jobs.status()}
    assert rows["backup"]["state"] == "ok" and rows["mail"]["state"] == "error"
    assert any("Почта отдела" in w and "ошибкой" in w for w in jobs.warnings(list(rows.values())))


def test_failed_run_keeps_previous_success(fake_jobs, monkeypatch):
    ok = jobs.run("backup")
    monkeypatch.setitem(jobs.JOBS, "backup", jobs.Job("backup", "Резервная копия", "bad.py", (), 24 * 60, 2880))
    jobs.run("backup")
    assert jobs.last("backup")["last_ok"] == ok["started"] and not jobs.last("backup")["ok"]


def test_late_never_and_off_states(fake_jobs, monkeypatch):
    rows = {r["key"]: r for r in jobs.status()}
    assert rows["backup"]["state"] == "never"
    assert any("Резервной копии ещё нет" in w for w in jobs.warnings(list(rows.values())))

    jobs.run("backup")
    three_days = time.time() + 3 * 24 * 3600
    row = {r["key"]: r for r in jobs.status(now=three_days)}["backup"]
    assert row["state"] == "late" and jobs.ago(row["age"]) == "3 дн назад"

    monkeypatch.setattr(jobs, "configured", lambda key: key == "backup")
    rows = {r["key"]: r for r in jobs.status(registered={"backup": True})}
    assert rows["mail"]["state"] == "off" and rows["backup"]["registered"] is True
    assert not any("Почта" in w for w in jobs.warnings(list(rows.values())))


def test_manual_backup_archive_counts(fake_jobs):
    (fake_jobs / "backups").mkdir()
    (fake_jobs / "backups" / "agent_backup_20261029_1200.zip").write_bytes(b"zip")
    row = {r["key"]: r for r in jobs.status()}["backup"]
    assert row["state"] == "ok" and row["age"] < 5


def test_job_periods_and_ages_read_well():
    assert [jobs.every(j.every) for j in jobs.JOBS.values()] == ["раз в день", "каждые 5 мин", "каждые 10 мин",
                                                                 "каждый час"]
    assert jobs.ago(None) == "ни разу" and jobs.ago(45) == "45 мин назад" and jobs.ago(300) == "5 ч назад"
    assert all((jobs.ROOT / "scripts" / j.script).exists() for j in jobs.JOBS.values())


def test_settings_file(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("﻿# папка отдела\nKB_DOCS_FOLDER=\\\\server\\docs\\ТКП\nKB_MAIL_HOST = \"mail.zavod.ru\"\n"
                    "OTHER=1\nбез знака равно\n", encoding="utf-8")
    assert config.read_settings_file(path) == {"KB_DOCS_FOLDER": "\\\\server\\docs\\ТКП", "KB_MAIL_HOST": "mail.zavod.ru"}
    assert config.read_settings_file(tmp_path / "нет.env") == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Планировщик заданий — только Windows")
def test_install_tasks_script_parses_and_dry_runs():
    import subprocess
    script = jobs.ROOT / "scripts" / "install_tasks.ps1"
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                          "-Jobs", "backup,mail", "-DryRun"], capture_output=True, timeout=120)
    text = out.stdout.decode("utf-8", errors="replace") + out.stdout.decode("cp866", errors="replace")
    assert out.returncode == 0, text
    assert "KBAgent-backup" in text and "KBAgent-mail" in text and "KBAgent-folder" not in text

"""Проверка ветки перед слиянием: сравнение прогонов по каждому вопросу и отчёт."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_branch", ROOT / "scripts" / "check_branch.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def csv_text(rows):
    lines = ["\ufeffid,group,type,question,seconds,correct"]
    lines += [f"{i},основной,doc,{q},{sec},{ok}" for i, q, sec, ok in rows]
    return "\n".join(lines) + "\n"


def test_regressions_fixes_and_new_questions_are_reported():
    base = cb.parse(csv_text([("Q1", "Срок входного контроля?", 1.2, True), ("Q2", "Кому звонить ночью?", 1.0, False),
                              ("Q3", "Срок поверки?", 1.4, True)]))
    new = cb.parse(csv_text([("Q1", "Срок входного контроля?", 1.1, False), ("Q2", "Кому звонить ночью?", 0.9, True),
                             ("Q4", "Новый вопрос", 2.0, False)]))
    c = cb.compare(base, new)
    assert (c["was"], c["now"]) == ("2 из 3", "1 из 3")
    assert [i for i, _ in c["broken"]] == ["Q1"] and [i for i, _ in c["fixed"]] == ["Q2"]
    assert c["added"] == [("Q4", "Новый вопрос", False)] and [i for i, _ in c["removed"]] == ["Q3"]
    text, clean = cb.report({"latest": c}, {"автотесты": True}, "main", "")
    assert not clean and "**сломался** Q1" in text and "починился Q2" in text and "новый, **неверно** Q4" in text
    assert "| эталон | 2 из 3 | 1 из 3 | 1 | 1 | 1 (1) | 1,2 → 1,1 |" in text


def test_clean_branch_and_set_without_baseline():
    rows = [("K01", "Письмо", 0.2, True), ("K02", "Письмо 2", 0.3, True)]
    same = cb.compare(cb.parse(csv_text(rows)), cb.parse(csv_text(rows)))
    fresh = cb.compare(None, cb.parse(csv_text(rows)))                    # набора не было в main
    assert fresh["was"] == "—" and len(fresh["added"]) == 2
    text, clean = cb.report({"tkp": same, "jury": fresh}, {"автотесты": True, "check_demo (сценарий показа)": True},
                            "main", "")
    assert clean and "можно сливать" in text
    _, failed = cb.report({"tkp": same}, {"автотесты": False}, "main", "")
    assert not failed

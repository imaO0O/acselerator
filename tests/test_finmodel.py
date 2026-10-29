"""Финансовая модель: формулы Excel совпадают с расчётом finmodel.py во всех сценариях; модель согласована."""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docs" / "ekonomika"))
fm = importlib.import_module("finmodel")


def test_model_is_consistent_with_passport_and_reality():
    base = fm.plan("базовый")
    assert [r["new"] for r in base] == [2, 5, 12]                       # план паспорта, п. 24
    assert base[1]["ebitda"] > -0.05e6 and base[2]["ebitda"] > 0         # безубыточность — 2028 г.
    assert all(0.3 < r["load"] <= 1.0 for r in base)                     # внедрение не перегружено и не простаивает
    cons, opt = fm.plan("консервативный"), fm.plan("оптимистичный")
    assert cons[-1]["cash"] < base[-1]["cash"] < opt[-1]["cash"]
    u = fm.unit()
    assert u["ltv_cac"] > 3 and u["payback_months"] < 12 and 0.5 < u["margin2_share"] < 0.95
    dept = fm.customer("отдел")
    assert dept["saved_per_request"] > dept["cost_per_request"]          # заказчику выгодно на каждом запросе


@pytest.mark.parametrize("scenario", fm.SCENARIOS)
def test_excel_formulas_match_python(tmp_path, scenario):
    formulas = pytest.importorskip("formulas")
    from openpyxl import load_workbook

    import build_finmodel as bf
    path = tmp_path / "model.xlsx"
    bf.build_xlsx(path)
    wb = load_workbook(path)
    wb["Допущения"]["D3"] = scenario
    wb.save(path)
    sol = formulas.ExcelModel().loads(str(path)).finish().calculate()

    def val(sheet, cell):
        key = next(k for k in sol if k.upper().endswith(f"{sheet.upper()}'!{cell}"))
        return float(sol[key].value[0][0])
    rows = {n: 5 + i for i, n in enumerate(bf.PLAN_ROWS)}
    for y, (col, r) in enumerate(zip("DEF", fm.plan(scenario))):
        for key, name in (("revenue", "Выручка"), ("ebitda", "EBITDA"), ("cash", "Деньги накопленно"),
                          ("tax", "Налог (УСН 6 % с вычетом взносов)"), ("load", "Загрузка внедрения")):
            assert val("План", f"{col}{rows[name]}") == pytest.approx(r[key], rel=1e-9, abs=1e-6), (name, y)
    u, urows = fm.unit(scenario=scenario), {n: 3 + i for i, n in enumerate(bf.UNIT_ROWS)}
    for key, name in (("ltv", "Ценность клиента за 5 лет (LTV)"), ("cac", "Стоимость привлечения (CAC)")):
        assert val("Клиент", f"B{urows[name]}") == pytest.approx(u[key], rel=1e-9)

"""Аналитика спроса: что запрашивают, что не закрыто и почему, причины проигрышей ТКП."""
from kbagent import demand, spravochnik as S, tkp

ALL = {"all", "production", "metrology", "admin"}
LETTER = ("Добрый день! Просим направить коммерческое предложение: 2 осциллографа с полосой не менее 150 МГц "
          "и 4 каналами; мультиметр для измерения постоянного тока до 15 А — 3 шт.; паяльная станция для пайки "
          "при 450 °C; источник питания 30 В 5 А.")


def test_client_letter_goes_to_demand_without_client_data(tmp_settings, tmp_path, monkeypatch):
    from kbagent import experts
    monkeypatch.setattr(experts, "PATH", tmp_path / "experts.json")
    positions = demand.from_tkp(tkp.parse(LETTER, {"all"}).to_dict())
    demand.record("письмо клиента", positions, "Техпродажи и сервис", "prodazhi")
    demand.record("письмо клиента", positions, "Техпродажи и сервис", "eval")        # прогон эталона — не спрос
    demand.record("письмо клиента", positions, "Техпродажи и сервис", None)
    records = demand.load()
    assert len(records) == 1
    assert "Добрый день" not in (tmp_settings.log_dir / "demand.jsonl").read_text(encoding="utf-8")
    s = demand.summary(records)
    assert s["positions"] == 4 and s["closed"] == 3 and s["closed_share"] == 75
    kinds = {k: (n, qty, closed) for k, n, qty, closed in s["kinds"]}
    assert kinds["Осциллограф"] == (1, 2, 1) and kinds["Мультиметр"] == (1, 3, 1)
    assert [why for _, why, _ in s["unmet"]] == ["нет в справочнике"]
    assert dict(s["offered"])["АКИП-4143/1"] == 2
    assert any(r.startswith("осциллограф: полоса пропускания не менее 150") for r, _ in s["reqs"])


def test_selection_and_model_check_go_to_demand_value_questions_do_not():
    values, _ = S.load()
    pick = S.answer(S.understand("Нужен осциллограф с полосой не менее 150 МГц и 4 каналами", values), ALL, "")
    [p] = demand.from_selection(pick.stats["demand"])
    assert p["kind"] == "Осциллограф" and p["status"] == "offer" and len(p["reqs"]) == 2
    check = S.answer(S.understand("Можно ли мультиметром MAS830L измерить ток 15 А?", values), ALL, "")
    [p] = demand.from_selection(check.stats["demand"])
    assert p["kind"] == "Мультиметр" and p["status"] == "no_fit" and p["asked_model"] == "MAS830L"
    assert p["model"] == "" and "15" in p["unmet"]
    value = S.answer(S.understand("Какая полоса пропускания у АКИП-4115/2В?", values), ALL, "")
    assert demand.from_selection(value.stats.get("demand")) == []
    s = demand.summary([{"positions": demand.from_selection(check.stats["demand"])}])
    assert s["asked"] == [("MAS830L", 1)] and s["unmet"][0][1].startswith("ни одна модель не подходит: ")


def test_lost_reasons_and_models_come_from_registry():
    def rec(number, status, models, comment="", date="2026-10-20 10:00"):
        return {"number": number, "status": status, "models": models, "date": date,
                "history": [{"status": "утверждён", "comment": ""}, {"status": status, "comment": comment}]}
    registry = [rec("1", "выигран", ["DT9205A"]), rec("2", "проигран", ["DT9205A", "SBK936B"], "дорого"),
                rec("3", "проигран", ["SBK936B"], " дорого ; у конкурента на 12 % дешевле"), rec("4", "проигран", ["X"]),
                rec("5", "заменён", ["DT9205A"]), rec("6", "проигран", ["Y"], "сроки", date="2026-01-10 10:00")]
    s = demand.summary([], registry)
    assert s["won"] == 1 and s["lost"] == 4
    assert s["lost_reasons"][0] == ("дорого", 2) and ("причина не указана", 1) in s["lost_reasons"]
    assert s["by_model"][0] == ("DT9205A", 1, 1)
    import datetime as dt
    assert demand.summary([], registry, since=dt.date(2026, 10, 1))["lost"] == 3

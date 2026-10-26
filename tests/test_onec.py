"""Цены и остатки из 1С (OData, только чтение) в прайс для ТКП — на имитации ответов «Бухгалтерии 3.0»."""
import pytest

from kbagent import onec, prajs

OPT, ROZN, RUB, USD, ACC = "t-opt", "t-rozn", "c-rub", "c-usd", "a-4101"
ITEMS = [
    {"Ref_Key": "i1", "Description": "Осциллограф цифровой АКИП-4143/1", "НаименованиеПолное": "", "Артикул": ""},
    {"Ref_Key": "i2", "Description": "Осциллограф цифровой АКИП-4143/1А", "НаименованиеПолное": "", "Артикул": ""},
    {"Ref_Key": "i3", "Description": "Мультиметр цифровой", "НаименованиеПолное": "", "Артикул": "DT9205A"},
    {"Ref_Key": "i4", "Description": "Паяльная станция CXG DES H95", "НаименованиеПолное": "", "Артикул": ""},
    {"Ref_Key": "i5", "Description": "Комплект АКИП-4143/1 и АКИП-4143/2", "НаименованиеПолное": "", "Артикул": ""},
    {"Ref_Key": "i6", "Description": "Кабель USB 1,5 м", "НаименованиеПолное": "", "Артикул": ""},
    {"Ref_Key": "i7", "Description": "Мультиметр MAS830L", "НаименованиеПолное": "", "Артикул": ""},
]
PRICES = [
    {"Period": "2026-03-12T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i1", "Валюта_Key": RUB, "Цена": 185000},
    {"Period": "2026-03-12T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i2", "Валюта_Key": RUB, "Цена": 201000},
    {"Period": "2026-09-01T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i3", "Валюта_Key": RUB, "Цена": 1390.5},
    {"Period": "2026-09-01T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i4", "Валюта_Key": RUB, "Цена": 7500},
    {"Period": "2026-09-01T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i5", "Валюта_Key": RUB, "Цена": 400000},
    {"Period": "2026-09-01T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i6", "Валюта_Key": RUB, "Цена": 300},
    {"Period": "2026-09-01T00:00:00", "ТипЦен_Key": OPT, "Номенклатура_Key": "i7", "Валюта_Key": USD, "Цена": 20},
]


class Resp:
    def __init__(self, status, value=None):
        self.status_code, self._value = status, value

    def json(self):
        return {"value": self._value}


class Fake1C:
    """Стандартный интерфейс OData «Бухгалтерии 3.0»: отвечает только на GET, запоминает запросы."""
    def __init__(self, status=200, vat=True):
        self.calls, self.status, self.vat = [], status, vat

    def get(self, url, params=None, auth=None, timeout=None):
        path = url.split("standard.odata/", 1)[1]
        self.calls.append((path, dict(params or {})))
        if self.status != 200:
            return Resp(self.status)
        data = {
            "Catalog_ТипыЦенНоменклатуры": [{"Ref_Key": OPT, "Description": "Оптовая", "ЦенаВключаетНДС": self.vat},
                                            {"Ref_Key": ROZN, "Description": "Розничная", "ЦенаВключаетНДС": True}],
            "Catalog_Номенклатура": ITEMS,
            "Catalog_Валюты": [{"Ref_Key": RUB, "Code": "643"}, {"Ref_Key": USD, "Code": "840"}],
            "InformationRegister_ЦеныНоменклатуры/SliceLast()": [p for p in PRICES if OPT in params.get("$filter", "")],
            "ChartOfAccounts_Хозрасчетный": [{"Ref_Key": ACC, "Code": "41.01"}],
            "AccountingRegister_Хозрасчетный/Balance()": [{"ExtDimension1": "i4", "КоличествоBalance": 3},
                                                          {"ExtDimension1": "i3", "КоличествоBalance": 0}],
        }[path]
        skip, top = int(params["$skip"]), int(params["$top"])
        return Resp(200, data[skip:skip + top])


@pytest.fixture
def one_c(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_1C_URL", "http://srv/buh/odata/standard.odata")
    monkeypatch.setenv("KB_1C_USER", "agent_readonly")
    monkeypatch.setenv("KB_1C_PASSWORD", "пароль-1С")
    monkeypatch.setenv("KB_1C_PRICE_TYPE", "оптовая")
    monkeypatch.setenv("KB_1C_STOCK", "1")
    monkeypatch.setattr(prajs, "PATH", tmp_path / "prajs.csv")
    monkeypatch.setattr(onec, "REPORT", tmp_path / "prajs_1c.json")
    prajs._CACHE.clear()
    yield
    prajs._CACHE.clear()


def test_nomenclature_is_matched_to_models_exactly():
    match = onec.matcher({"АКИП-4143/1", "АКИП-4143/1А", "АКИП-4143/2", "DT9205A", "CXG DES H95"})
    assert match("", "Осциллограф цифровой АКИП-4143/1") == ("АКИП-4143/1", [])
    assert match("", "Осциллограф АКИП 4143/1А") == ("АКИП-4143/1А", [])               # не «/1»
    assert match("DT9205A", "Мультиметр цифровой") == ("DT9205A", [])                 # по артикулу
    assert match("", "Паяльная станция CXG DES H95") == ("CXG DES H95", [])
    assert match("", "Комплект АКИП-4143/1 и АКИП-4143/2") == (None, ["АКИП-4143/1", "АКИП-4143/2"])
    assert match("", "Кабель USB 1,5 м") == (None, [])


def test_prices_and_stock_come_from_1c_into_price_list(one_c, monkeypatch):
    from kbagent.config import settings
    monkeypatch.setattr(settings, "prices_include_vat", True)
    (prajs.PATH).write_text("Модель;Цена;Срок поставки;Действует с;Примечание\n"
                            "DT9205A;1450;10 рабочих дней;01.10.2026;\n", encoding="utf-8-sig")
    fake = Fake1C()
    rep = onec.sync(session=fake)
    assert fake.calls and not hasattr(fake, "post")          # у имитации 1С есть только GET — запись невозможна
    prices, _ = prajs.load()
    assert prices["АКИП-4143/1"].price == 185000 and prices["АКИП-4143/1А"].price == 201000
    assert prices["DT9205A"].price == 1390.5 and prices["DT9205A"].lead == "10 рабочих дней"   # срок — от менеджера
    assert prices["CXG DES H95"].lead == "в наличии (3 шт.)"                                 # остаток на 41.01
    assert not prices["АКИП-4143/1"].stale and "цена от 12.03.2026" in prices["АКИП-4143/1"].note
    assert "MAS830L" not in prices                                                            # цена в долларах
    assert rep["matched"] == 4 and rep["unmatched_items"] == 1 and rep["foreign"] == ["MAS830L (Мультиметр MAS830L)"]
    assert rep["ambiguous"] == ["Комплект АКИП-4143/1 и АКИП-4143/2 → АКИП-4143/1, АКИП-4143/2"]
    assert rep["warnings"] == [] and "MAS830L" in rep["without_price"] and rep["server"] == "srv"
    assert "пароль-1С" not in onec.REPORT.read_text(encoding="utf-8")                      # пароля в отчёте нет
    assert onec.last_report()["prices"] == 4


def test_vat_mismatch_paging_and_errors_are_explained(one_c, monkeypatch):
    from kbagent.config import settings
    monkeypatch.setattr(settings, "prices_include_vat", True)
    monkeypatch.setattr(onec, "PAGE", 2)                                   # постраничная выборка
    fake = Fake1C(vat=False)
    rep = onec.sync(session=fake)
    assert rep["matched"] == 4 and "без НДС" in rep["warnings"][0]
    assert [p["$skip"] for path, p in fake.calls if path == "Catalog_Номенклатура"] == [0, 2, 4, 6]
    with pytest.raises(onec.OneCError, match="отклонила вход"):
        onec.sync(session=Fake1C(status=401))
    with pytest.raises(onec.OneCError, match="не опубликован «Catalog_ТипыЦенНоменклатуры»"):
        onec.sync(session=Fake1C(status=404))
    monkeypatch.setenv("KB_1C_PRICE_TYPE", "Закупочная")
    with pytest.raises(onec.OneCError, match="нет типа цен «Закупочная». Есть: Оптовая, Розничная"):
        onec.sync(session=Fake1C())
    monkeypatch.delenv("KB_1C_URL")
    assert not onec.configured()

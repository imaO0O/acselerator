"""Trade catalog import, conservative matching and large SQLite catalog checks."""
import csv
import hashlib
import io
import sqlite3
import time
from contextlib import closing
from decimal import Decimal

import pytest
from openpyxl import Workbook

from kbagent.trade_catalog import (CatalogValidationError, TradeCatalog, decimal_value,
                                   normalize_article, preview_table)


def table(headers, rows, delimiter=";"):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


@pytest.fixture
def catalog(tmp_path):
    return TradeCatalog(tmp_path / "catalog.sqlite")


def add(backend, rows, headers=("article", "name", "price"), **kwargs):
    return backend.import_table("offers.csv", table(headers, rows),
                                {h: h for h in headers}, **kwargs)


@pytest.mark.parametrize("value,expected", [
    ("1 234,50", Decimal("1234.50")), ("1\u00a0234.500", Decimal("1234.500")),
    ("1\u202f234", Decimal(1234)), (0, Decimal(0)), ("+2,5", Decimal("2.5")),
    (Decimal("0.125"), Decimal("0.125")), (None, None), ("", None),
    (0.0000001, Decimal("0.0000001")), (Decimal("1e-8"), Decimal(".00000001")),
])
def test_decimal_values(value, expected):
    assert decimal_value(value) == expected


@pytest.mark.parametrize("value", ["NaN", "Inf", float("inf"), "-1", True,
                                         "1,234.50", "1 2", "5 шт", "=1+2",
                                         "1e6", "0.0000000000001", "1000000000000001"])
def test_invalid_decimals(value):
    with pytest.raises(ValueError):
        decimal_value(value)


def test_article_normalization():
    assert normalize_article(" аВ-с 12\u201112 ") == normalize_article("AB-C1212")
    assert normalize_article("AB/12") != normalize_article("AB12")
    assert normalize_article("Б12") != normalize_article("B12")


def test_missing_database_is_read_only(catalog):
    assert catalog.search("A") == []
    assert catalog.options() == {"catalogs": [], "brands": [], "groups": []}
    assert not catalog.path.exists()
    assert list(catalog.path.parent.iterdir()) == []


def test_csv_preview_and_configurable_mapping(catalog):
    data = table(["Код", "Товар", "Цена", "Остаток", "НДС"],
                 [["АВ-12", "Фильтр", "1 234,50", "2,5", "20%"]])
    preview = catalog.preview_table("offers.csv", data)
    assert preview_table("offers.csv", data) == preview
    assert preview == {"columns": ["Код", "Товар", "Цена", "Остаток", "НДС"],
                       "rows": [{"Код": "АВ-12", "Товар": "Фильтр", "Цена": "1 234,50",
                                 "Остаток": "2,5", "НДС": "20%"}], "sheets": []}
    assert not catalog.path.exists()
    report = catalog.import_table("folder/offers.csv", data,
                                  {"article": "Код", "name": "Товар", "price": "Цена",
                                   "quantity": "Остаток", "vat_rate": "НДС"})
    assert report["imported"] == 1
    hit, = catalog.search("ab 12")
    assert hit["price"] == 1234.5 and hit["stock"] == 2.5 and hit["vat_rate"] == 20
    assert hit["source"] == "offers.csv" and hit["source_row"] == 2
    assert hit["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert hit["imported_at"] == hit["created_at"] == hit["updated_at"]
    assert hit["match_type"] == "exact" and hit["needs_review"] is False
    required = {"id", "article", "name", "brand", "group", "catalog", "price", "stock",
                "lead_time", "vat_rate", "source", "source_row", "match_type", "needs_review", "applicability"}
    assert required <= hit.keys()
    assert isinstance(hit["applicability"], str)


def test_inverse_mapping_cp1251_and_preview_bound(catalog):
    data = table(["Код", "Товар"], [[str(i), "Насос"] for i in range(30)]).decode("utf-8-sig").encode("cp1251")
    assert len(catalog.preview_table("a.csv", data)["rows"]) == 20
    result = catalog.import_table("a.csv", data, {"Код": "article", "Товар": "name"})
    assert result["imported"] == 30
    hit, = catalog.search("29")
    assert hit["price"] is None and hit["needs_review"]


def xlsx_bytes(formula=True):
    book = Workbook()
    book.active.title = "Instructions"
    book.active.append(["Note"])
    sheet = book.create_sheet("Prices")
    sheet.append(["SKU", "Name", "Cost", "Stock"])
    sheet.append(["0012", "Pump", 12.25, 4])
    sheet.append(["formula", "Invalid", "=2+2" if formula else 4, 1])
    sheet.append([None, None, None, None])
    sheet.append(["0013", "Valve", 0, 0])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def test_xlsx_sheets_formulas_and_source_rows(catalog):
    data = xlsx_bytes()
    assert catalog.preview_table("x.xlsx", data)["sheets"] == ["Instructions", "Prices"]
    assert catalog.preview_table("x.xlsx", data, "Prices")["columns"] == ["SKU", "Name", "Cost", "Stock"]
    mapping = {"article": "SKU", "name": "Name", "price": "Cost", "stock": "Stock"}
    with pytest.raises(CatalogValidationError) as caught:
        catalog.import_table("x.xlsx", data, mapping, sheet="Prices")
    assert caught.value.report["errors"][0]["source_row"] == 3
    assert not catalog.search("0012")
    report = catalog.import_table("x.xlsx", xlsx_bytes(False), mapping, sheet="Prices")
    assert report["imported"] == 3 and report["committed"]
    hit, = catalog.search("0013")
    assert hit["source_row"] == 5 and hit["sheet"] == "Prices" and hit["price"] == 0
    with pytest.raises(ValueError, match="unknown sheet"):
        catalog.preview_table("x.xlsx", data, "Absent")


@pytest.mark.parametrize("headers,mapping", [
    (["SKU", "SKU"], {"article": "SKU", "name": "SKU"}),
    (["SKU", ""], {"article": "SKU", "name": "Missing"}),
    (["SKU"], {"article": "SKU"}),
    (["SKU", "Title"], {"article": "SKU", "name": "Missing"}),
    (["SKU", "Title"], {"article": "SKU", "name": "Title", "bad": "Title"}),
])
def test_invalid_mapping_does_not_create_database(catalog, headers, mapping):
    with pytest.raises(ValueError):
        catalog.import_table("a.csv", table(headers, [["1", "x"]]), mapping)
    assert not catalog.path.exists()


def test_row_validation_and_diagnostic_bound(catalog):
    headers = ["article", "name", "price", "stock", "vat_rate", "year_from", "year_to"]
    rows = [["A", "Valid", "0", "0.125", "0", "2000", "2010"],
            ["B", "Bad price", "-1", "1", "20", "", ""],
            ["C", "Bad VAT", "1", "1", "101", "", ""],
            ["D", "Bad stock", "1", "NaN", "20", "", ""],
            ["E", "Bad years", "1", "1", "20", "2010", "2000"],
            ["", "Empty article", "1", "1", "20", "", ""]]
    add(catalog, [rows[0]], headers)
    with pytest.raises(CatalogValidationError) as caught:
        add(catalog, rows, headers)
    report = caught.value.report
    assert report["imported"] == 0 and report["rejected"] == 5 and report["validated"] == 1
    assert not report["committed"]
    assert [e["source_row"] for e in report["errors"]] == [3, 4, 5, 6, 7]
    with pytest.raises(CatalogValidationError) as caught:
        add(catalog, [["bad", "Bad", "NaN"]] * 120)
    report = caught.value.report
    assert report["rejected"] == 120 and len(report["errors"]) == 100
    assert catalog.search("A")[0]["stock"] == .125


def test_collisions_filters_availability_and_options(catalog):
    headers = ["article", "name", "price", "brand", "group", "stock", "lead_time"]
    add(catalog, [["AB-12", "Pump", 10, "Bosch", "Pumps", 2, "today"],
                  ["АВ12", "Pump", 11, "Other", "Pumps", 0, "10 days"]], headers)
    add(catalog, [["AB12", "Pump", 12, "Bosch", "Pumps", 3, "tomorrow"]], headers, catalog="Second")
    assert catalog.search("AB12", limit=1)[0]["needs_review"]
    assert len(catalog.search("AB12", available_only=True)) == 2
    hit, = catalog.search("AB12", brand=" BOSCH ", catalog="основной", group="pumps")
    assert hit["price"] == 10 and hit["lead_time"] == "today" and not hit["needs_review"]
    assert catalog.search("AB12", brand="Other", available_only=True) == []
    assert catalog.options() == {"catalogs": ["Second", "Основной"], "brands": ["Bosch", "Other"],
                                 "groups": ["Pumps"]}


def test_cross_references_are_explicit_and_exact_has_priority(catalog):
    add(catalog, [["P100", "Pump", 10, "OLD-100;OEM-200|OTHER-300\nFOUR-400"]],
        ["article", "name", "price", "cross_refs"])
    for query in ("old100", "oem200", "other300", "four400"):
        hit, = catalog.search(query)
        assert hit["article"] == "P100" and hit["match_type"] == "cross"
        assert hit["needs_review"]
    assert catalog.search("UNRELATED999") == []
    add(catalog, [["OLD100", "Original", 11]], catalog="Originals")
    hit, cross = catalog.search("OLD100")
    assert hit["article"] == "OLD100" and hit["match_type"] == "exact"
    assert cross["article"] == "P100" and cross["needs_review"]
    add(catalog, [["P200", "Pump", 12, "OEM200"]], ["article", "name", "price", "cross_refs"], catalog="Alternatives")
    assert catalog.search("OEM200", limit=1)[0]["needs_review"]


@pytest.mark.parametrize("delimiter", [";", ",", "\t"])
def test_csv_delimiter_with_quoted_multiline_crosses(catalog, delimiter):
    headers = ["article", "name", "price", "cross_refs"]
    data = table(headers, [["P100", "Pump", "10.25", "OLD-100;OEM-200|OTHER-300\nFOUR-400"]], delimiter)
    preview = preview_table("catalog.csv", data)
    assert preview["columns"] == headers
    assert preview["rows"][0]["cross_refs"] == "OLD-100;OEM-200|OTHER-300\nFOUR-400"
    catalog.import_table("catalog.csv", data, {h: h for h in headers})
    assert catalog.search("FOUR400")[0]["article"] == "P100"


def test_csv_delimiter_with_commas_in_semicolon_headers(catalog):
    data = table(["SKU", "Price, USD, net", "Title"], [["A", "10,25", "Pump"]])
    assert preview_table("catalog.csv", data)["columns"] == ["SKU", "Price, USD, net", "Title"]


def test_description_and_typo_candidates_never_approved(catalog):
    add(catalog, [["FILTER1234", "Oil filter engine", 10], ["OTHER999", "Oil filter fuel", 12]])
    hits = catalog.search("oil filter")
    assert len(hits) == 2
    assert all(h["match_type"] == "description" and h["needs_review"] for h in hits)
    for typo in ("FILTER1324", "XILTER1234"):
        hit, = catalog.search(typo)
        assert hit["article"] == "FILTER1234" and hit["match_type"] == "fuzzy" and hit["needs_review"]
        assert hit["applicability"] == ""


@pytest.mark.parametrize("query", ["масляный фильтр на ДЕМО-Т82", "нужен масляный фильтр для ДЕМО-Т82",
                                        "нужны масляный фильтр на ДЕМО-Т82", "подбери масляный фильтр для ДЕМО-Т82"])
def test_description_indexes_only_explicit_applicability_and_model(catalog, query):
    add(catalog, [["FILTER100", "Масляный фильтр", 10, "ДЕМО-Т82", ""],
                  ["FILTER200", "Масляный фильтр", 12, "", "ДЕМО-Т82"],
                  ["FILTER300", "Масляный фильтр", 13, "ДЕМО-Т90", ""],
                  ["FILTER400", "Масляный фильтр", 14, "", ""]],
        ["article", "name", "price", "model", "applicability"])
    hits = catalog.search(query)
    assert {hit["article"] for hit in hits} == {"FILTER100", "FILTER200"}
    assert all(hit["match_type"] == "description" and hit["needs_review"] for hit in hits)
    assert catalog.search("нужен подбери для на") == []


def test_extracted_article_with_unsupported_context_requires_review(catalog):
    add(catalog, [["AB123", "Oil pump", 10]])
    hit, = catalog.search("AB123 Oil pump for unsupported vehicle")
    assert hit["match_type"] == "exact" and hit["needs_review"]


def test_equipment_article_in_description_does_not_approve_equipment_as_part(catalog):
    add(catalog, [["ДЕМО-Т82", "Трактор", 100, ""],
                  ["FILTER100", "Масляный фильтр", 10, "ДЕМО-Т82"]],
        ["article", "name", "price", "model"])
    hits = catalog.search("масляный фильтр на ДЕМО-Т82")
    assert [(hit["article"], hit["match_type"]) for hit in hits] == [
        ("ДЕМО-Т82", "exact"), ("FILTER100", "description")]
    assert all(hit["needs_review"] for hit in hits)


def test_explicit_applicability_no_vin_inference(catalog):
    headers = ["article", "name", "price", "mark", "model", "year_from", "year_to", "vin", "applicability"]
    vin = "WVWZZZ1JZXW000001"
    add(catalog, [["A100", "Pump", 10, "VW", "Golf", 1998, 2005, vin, "Supplier fitment"],
                  ["A100", "Pump", 12, "", "", "", "", "", "Golf 1998-2005 in description only"]], headers)
    for model in ("Golf", "VW", "VW Golf"):
        hit, = catalog.search("A100", model=model, year=2000, vin=vin.lower())
        assert "Supplier fitment" in hit["applicability"] and not hit["needs_review"]
    assert catalog.search("A100", model="Polo") == []
    assert catalog.search("A100", year=2006) == []
    assert catalog.search("A100", vin="WVWZZZ1JZXW000002") == []
    assert catalog.search("Golf", vin="WVWZZZ1JZXW000002") == []
    with pytest.raises(ValueError, match="VIN"):
        catalog.search("A100", vin="partial")


def test_customer_prices_discounts_missing_levels_and_decimal_rounding(catalog):
    add(catalog, [["A", "Pump", "100.05", "80.25", "10%", "5%"]],
        ["article", "name", "price", "price:Partner", "discount:Partner", "discount:Retail"])
    assert catalog.search("A")[0]["price"] == 100.05
    assert catalog.search("A", price_level="PARTNER", discount="5")[0]["price"] == 68.61
    assert catalog.search("A", price_level="retail")[0]["price"] == 95.05
    assert catalog.search("A", discount=100)[0]["price"] == 0
    hit, = catalog.search("A", price_level="unknown")
    assert hit["price"] is None and hit["needs_review"]
    assert catalog.search("A")[0]["price"] == 100.05  # no persistence of customer discounts


def test_named_customer_rows_do_not_leak_to_other_customers(catalog):
    add(catalog, [["A", "Pump", 50, "Customer1"]], ["article", "name", "price", "price_level"])
    assert catalog.search("A") == []
    assert catalog.search("A", price_level="Customer2") == []
    assert catalog.search("A", price_level="customer1")[0]["price"] == 50


def test_per_row_customer_level_overrides_base_for_same_product(catalog):
    add(catalog, [["A100", "Pump", 100, ""], ["A100", "Pump", 50, "Customer1"],
                  ["A100", "Pump", 60, "Customer2"]], ["article", "name", "price", "price_level"])
    assert catalog.search("A100")[0]["price"] == 100
    hit, = catalog.search("A100", price_level="Customer1", discount=10)
    assert hit["price"] == 45 and not hit["needs_review"]
    assert catalog.search("A100", price_level="Customer2")[0]["price"] == 60


def test_transactional_replacement_refreshes_stock_price_and_all_search_indexes(catalog):
    headers = ["article", "name", "price", "stock", "cross_numbers", "model"]
    first = add(catalog, [["OLD100", "Obsolete part", 10, 20, "OLD-CROSS", "Old model"]], headers)
    retained = add(catalog, [["OTHER100", "Other supplier", 11, 2, "OTHER-CROSS", "Other model"]],
                   headers, catalog="Other")
    updated = add(catalog, [["NEW100", "Fresh part", 25, 0, "NEW-CROSS", "New model"]], headers)
    assert first["committed"] and first["replaced"] == 0
    assert updated["committed"] and updated["replaced"] == 1
    assert updated["import_id"] != first["import_id"]
    assert catalog.search("OLD100", catalog="Основной") == []
    assert catalog.search("OLD-CROSS") == []
    assert catalog.search("Obsolete") == []
    assert catalog.search("NEW100", model="Old model") == []
    hit, = catalog.search("NEW100")
    assert hit["price"] == 25 and hit["stock"] == 0
    assert catalog.search("OTHER100")[0]["price"] == 11
    with closing(catalog._reader()) as db:
        for report, expected in ((first, 0), (retained, 1), (updated, 1)):
            assert db.execute("SELECT COUNT(*) FROM items WHERE import_id=?",
                              (report["import_id"],)).fetchone()[0] == expected
        for child in ("cross_refs", "fitment", "name_terms"):
            assert db.execute(f"SELECT COUNT(*) FROM {child} c LEFT JOIN items i ON c.item_id=i.id "
                              "WHERE i.id IS NULL").fetchone()[0] == 0


def test_invalid_refresh_preserves_previous_catalog_hash_and_offers(catalog):
    previous = add(catalog, [["A100", "Pump", 10]])
    with pytest.raises(CatalogValidationError) as caught:
        add(catalog, [["A100", "Pump", 999], ["BAD", "Pump", "invalid"]])
    assert not caught.value.report["committed"] and caught.value.report["imported"] == 0
    hit, = catalog.search("A100")
    assert hit["price"] == 10 and hit["imported_at"] == previous["imported_at"]
    with closing(catalog._reader()) as db:
        assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
        assert db.execute("SELECT sha256 FROM imports").fetchone()[0] == hit["source_sha256"]
    with pytest.raises(ValueError, match="no data rows"):
        add(catalog, [])
    assert catalog.search("A100")[0]["id"] == hit["id"]


def test_batch_article_and_description_exact_and_unknown_stock(catalog):
    from kbagent.trade_requests import match_batch
    add(catalog, [["AB-123", "Oil pump", 10], ["CD-456", "Air filter", 20]])
    for query in ("AB-123 Oil pump", "AB 123 Oil pump", "Oil pump AB-123"):
        hit, = catalog.search(query)
        assert hit["article"] == "AB-123" and hit["match_type"] == "exact" and not hit["needs_review"]
    result, = match_batch(catalog, [{"query": "AB-123 Oil pump", "qty": 2}])
    assert result["status"] == "exact" and result["selected"]["stock"] is None
    hits = catalog.search("AB-123 CD-456 Oil pump", limit=1)
    assert hits[0]["needs_review"]


def test_out_of_stock_batch_has_reviewed_explicit_cross_candidates(catalog):
    from kbagent.trade_requests import match_batch
    add(catalog, [["ORIG100", "Oil pump", 10, 0, "ALT100"],
                  ["ALT100", "Oil pump", 12, 10, "THIRD100"],
                  ["THIRD100", "Oil pump", 13, 10, ""]],
        ["article", "name", "price", "stock", "cross_numbers"])
    result, = match_batch(catalog, [{"query": "ORIG100 Oil pump", "qty": 1}])
    assert result["status"] == "out_of_stock" and result["selected"]["article"] == "ORIG100"
    assert [(r["article"], r["match_type"], r["needs_review"]) for r in result["candidates"]] == [
        ("ORIG100", "exact", False), ("ALT100", "cross", True)]
    hit, = catalog.search("ORIG100", available_only=True)
    assert hit["article"] == "ALT100" and hit["match_type"] == "cross" and hit["needs_review"]
    assert all(r["article"] != "THIRD100" for r in result["candidates"])


def test_symmetric_one_hop_crosses_respect_catalog_and_brand_filters(catalog):
    headers = ["article", "name", "price", "stock", "cross_numbers", "brand"]
    add(catalog, [["A100", "Pump", 10, 0, "B100", "Original"],
                  ["B100", "Pump", 12, 5, "", "Replacement"],
                  ["C100", "Pump", 13, 5, "B100", "Third"]], headers)
    add(catalog, [["B100", "Other catalog pump", 99, 5, "", "Replacement"]],
        headers, catalog="Other")
    forward = catalog.search("A100", catalog="Основной")
    assert [(r["article"], r["match_type"]) for r in forward] == [
        ("A100", "exact"), ("B100", "cross")]
    assert forward[0]["stock"] == 0 and forward[1]["stock"] == 5
    assert forward[1]["needs_review"]
    reverse = catalog.search("B100", catalog="Основной")
    assert [(r["article"], r["match_type"]) for r in reverse] == [
        ("B100", "exact"), ("A100", "cross"), ("C100", "cross")]
    assert all(r["needs_review"] for r in reverse[1:])
    hit, = catalog.search("A100", catalog="Основной", brand="Replacement", available_only=True)
    assert hit["article"] == "B100" and hit["price"] == 12 and hit["match_type"] == "cross"
    hit, = catalog.search("B100", catalog="Основной", brand="Original")
    assert hit["article"] == "A100" and hit["match_type"] == "cross"
    assert catalog.search("A100", catalog="Основной", brand="Third") == []


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": True}, {"limit": 1.5},
                                         {"discount": -1}, {"discount": 101}, {"discount": "NaN"},
                                         {"year": "2000 OR 1=1"}, {"year": 1999.5}])
def test_invalid_search_arguments(catalog, kwargs):
    with pytest.raises(ValueError):
        catalog.search("A", **kwargs)


def test_parameterized_search_and_read_only_connection(catalog):
    add(catalog, [["A100", "Pump", 10]])
    before = catalog.path.read_bytes()
    assert catalog.search("' OR 1=1 --", brand="' OR 1=1 --") == []
    with closing(catalog._reader()) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM items")
    assert catalog.path.read_bytes() == before
    assert catalog.search("A100")[0]["price"] == 10


def test_reader_works_while_writer_is_open(catalog):
    add(catalog, [["A100", "Pump", 10]])
    with closing(sqlite3.connect(catalog.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE items SET price='999'")
        assert catalog.search("A100")[0]["price"] == 10
        writer.rollback()


def test_atomic_rollback_on_structural_failure(catalog, monkeypatch):
    add(catalog, [["original", "Pump", 10]])
    import kbagent.trade_catalog as module
    monkeypatch.setattr(module, "_MAX_ROWS", 1)
    with pytest.raises(ValueError, match="500000"):
        add(catalog, [["new1", "Valve", 12], ["new2", "Valve", 13]])
    assert catalog.search("new1") == []
    assert catalog.search("original")[0]["price"] == 10
    with closing(sqlite3.connect(catalog.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_100k_catalog_exact_index_and_performance(catalog):
    data = table(["article", "name", "price", "brand", "stock"],
                 ((f"SKU{i:06d}", f"Part {i}", "12.34", "Maker", 1) for i in range(100_000)))
    report = catalog.import_table("large.csv", data,
                                  {h: h for h in ("article", "name", "price", "brand", "stock")})
    assert report["imported"] == 100_000 and report["rejected"] == 0
    with closing(catalog._reader()) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM items WHERE article_norm=?", ("SKU099999",)).fetchall()
        assert any("items_article_idx" in str(tuple(row)) and "SEARCH" in str(tuple(row)) for row in plan)
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 100_000
        # Actual search must also use the index, not just the simplified query.
        full_plan = db.execute("EXPLAIN QUERY PLAN SELECT i.* FROM items i JOIN imports p ON p.id=i.import_id "
                               "WHERE i.article_norm=? AND (i.price_level='' OR i.price_level=?) ORDER BY i.id LIMIT ?",
                               ("SKU099999", "", 11)).fetchall()
        assert any("items_article_idx" in str(tuple(row)) for row in full_plan)
    started = time.perf_counter()
    for i in range(100):
        hit, = catalog.search(f"sku-{99_900 + i:06d}", brand="maker", available_only=True)
        assert hit["price"] == 12.34 and hit["match_type"] == "exact" and not hit["needs_review"]
    # Generous wall-clock guard for CI; B-tree query plan above is the main guarantee.
    assert time.perf_counter() - started < 10

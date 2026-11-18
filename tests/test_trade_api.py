"""Isolated trade router: authentication, role checks, import limits and trust boundary."""
import base64
import hashlib
import io
from zipfile import ZipFile

import pytest
from docx import Document
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from api import trade
from kbagent.trade_catalog import TradeCatalog


SELLER = "Техпродажи и сервис"
ADMIN = "Администратор"
SECRET = "private-key-hash-do-not-return"
CSV = ("article;name;price;stock;vat_rate;cross_refs;brand;group\n"
       "A-1;Фильтр;120;10;20;;Brand;Parts\n"
       "B-2;Аналог;60;5;10;OLD-2;Other;Parts\n"
       "C-3;Нет цены;;1;;;;\n").encode("utf-8")
MAPPING = {k: k for k in ("article", "name", "price", "stock", "vat_rate", "cross_refs", "brand", "group")}


def dummy_client():
    raise HTTPException(401, "Authentication required")


def authenticated(x_test_role: str | None = Header(None)):
    if x_test_role is None:
        raise HTTPException(401, "Authentication required")
    role = {"seller": SELLER, "admin": ADMIN}.get(x_test_role, x_test_role)
    return {"role": role, "name": "Test user", "hash": SECRET, "clearance": 0}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(trade, "ROOT", tmp_path)
    monkeypatch.setattr(trade.settings, "tkp_roles", {SELLER, ADMIN})
    app = FastAPI()
    app.include_router(trade.build_router(dummy_client))
    app.dependency_overrides[dummy_client] = authenticated
    with TestClient(app) as client:
        yield client


def headers(role=SELLER):
    # HTTP headers cannot contain Cyrillic; use a fixture dependency override
    # with a small ASCII transport vocabulary.
    return {"X-Test-Role": {SELLER: "seller", ADMIN: "admin"}.get(role, role)}


def upload(**updates):
    result = {"filename": "offers.csv", "content_base64": base64.b64encode(CSV).decode(),
              "mapping": MAPPING, "catalog": "Supplier"}
    result.update(updates)
    return result


def seed(api):
    response = api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload())
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 3


OPERATIONS = [
    ("GET", "/trade/catalogs", None),
    ("POST", "/trade/search", {"query": "A-1"}),
    ("POST", "/trade/select", {"items": [{"query": "A-1", "qty": 1}]}),
    ("POST", "/trade/catalog/import", upload()),
    ("POST", "/trade/export", {"items": [{"query": "A-1", "qty": 1}]}),
]


@pytest.mark.parametrize("method,path,body", OPERATIONS)
def test_every_operation_requires_authentication_and_role(api, method, path, body):
    assert api.request(method, path, json=body).status_code == 401
    denied = api.request(method, path, json=body, headers=headers("visitor"))
    assert denied.status_code == 403
    assert SECRET not in denied.text


def test_import_admin_only_independent_of_trade_roles(api, monkeypatch):
    assert api.post("/trade/catalog/import", headers=headers(), json=upload()).status_code == 403
    monkeypatch.setattr(trade.settings, "tkp_roles", {SELLER})
    seed(api)  # admin can import even if not a trade role
    assert api.get("/trade/catalogs", headers=headers(ADMIN)).status_code == 403
    assert not (trade.ROOT / "catalog.sqlite").exists()
    assert (trade.ROOT / "data" / "trade" / "catalog.sqlite").exists()


def test_runtime_role_changes_take_effect(api, monkeypatch):
    assert api.get("/trade/catalogs", headers=headers()).status_code == 200
    monkeypatch.setattr(trade.settings, "tkp_roles", set())
    assert api.post("/trade/search", headers=headers(), json={"query": "A"}).status_code == 403


def test_catalogs_search_filters_and_batch(api):
    assert api.get("/trade/catalogs", headers=headers()).json() == {"catalogs": [], "brands": [], "groups": []}
    seed(api)
    options = api.get("/trade/catalogs", headers=headers())
    assert options.json()["catalogs"] == ["Supplier"]
    response = api.post("/trade/search", headers=headers(), json={
        "query": " A-1 ", "filters": {"brand": "Brand", "catalog": "Supplier", "limit": 1}})
    hit, = response.json()
    assert hit["price"] == 120 and hit["source"] == "offers.csv" and hit["source_row"] == 2
    assert hit["match_type"] == "exact"
    assert SECRET not in response.text
    assert api.post("/trade/search", headers=headers(), json={
        "query": "A-1", "filters": {"brand": "Other"}}).json() == []
    response = api.post("/trade/select", headers=headers(), json={"items": [
        {"query": "A-1", "qty": 2.5}, {"query": "A-1", "qty": 11},
        {"query": "OLD-2", "qty": 1}]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert [r["status"] for r in result] == ["exact", "out_of_stock", "review"]
    assert result[0]["qty"] == 2.5
    assert result[2]["selected"] is None
    assert result[2]["candidates"][0]["article"] == "B-2"


@pytest.mark.parametrize("path,body", [
    ("/trade/search", {"query": " "}),
    ("/trade/search", {"query": "a" * 201}),
    ("/trade/search", {"query": "A", "filters": {"limit": 101}}),
    ("/trade/search", {"query": "A", "filters": {"limit": True}}),
    ("/trade/search", {"query": "A", "filters": {"discount": 100}}),
    ("/trade/search", {"query": "A", "filters": {"vin": "invalid"}}),
    ("/trade/select", {"items": []}),
    ("/trade/select", {"items": [{"query": "A"}] * 101}),
    ("/trade/select", {"items": [{"query": "A", "qty": 0}]}),
    ("/trade/select", {"items": [{"query": "A", "qty": True}]}),
    ("/trade/select", {"items": [{"query": "A", "qty": "NaN"}]}),
    ("/trade/select", {"items": [{"query": "A", "qty": 1_000_001}]}),
    ("/trade/select", {"items": [{"query": "a" * 201}]}),
])
def test_bounded_queries_and_batches(api, path, body):
    assert api.post(path, headers=headers(), json=body).status_code == 422


def test_batch_accepts_100_without_dropping_rows(api):
    result = api.post("/trade/select", headers=headers(), json={"items": [{"query": "A"}] * 100})
    assert result.status_code == 200 and len(result.json()) == 100


@pytest.mark.parametrize("value", ["@@@", "YWJj\n", "YWJj$", "é", "YQ", ""])
def test_strict_base64(api, value):
    assert api.post("/trade/catalog/import", headers=headers(ADMIN),
                    json=upload(content_base64=value)).status_code == 422
    assert not (trade.ROOT / "data" / "trade" / "catalog.sqlite").exists()


@pytest.mark.parametrize("name", ["../offers.csv", "C:\\secret.csv", "dir/offers.csv", "offers.xls", "offers.exe"])
def test_import_rejects_paths_and_unsupported_formats(api, name):
    assert api.post("/trade/catalog/import", headers=headers(ADMIN),
                    json=upload(filename=name)).status_code == 422


def test_import_size_limit_without_allocating_100mb(api, monkeypatch):
    assert trade.MAX_IMPORT_BYTES == 100 * 1024 * 1024
    schema = trade.ImportRequest.model_json_schema()
    assert schema["properties"]["content_base64"]["maxLength"] == trade.MAX_BASE64_LENGTH
    monkeypatch.setattr(trade, "MAX_IMPORT_BYTES", len(CSV) - 1)
    assert api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload()).status_code == 413
    monkeypatch.setattr(trade, "MAX_IMPORT_BYTES", len(CSV))
    seed(api)


def test_malformed_xlsx_and_bad_mapping_are_safe_client_errors(api):
    response = api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload(
        filename="broken.xlsx", content_base64=base64.b64encode(SECRET.encode()).decode()))
    assert response.status_code == 422 and SECRET not in response.text
    response = api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload(
        mapping={"article": "missing", "name": SECRET}))
    assert response.status_code == 422 and SECRET not in response.text


def test_xlsx_import_sheet_and_mapping(api):
    book = Workbook()
    book.active.title = "Ignore"
    sheet = book.create_sheet("Offers")
    sheet.append(["Code", "Description", "Cost"])
    sheet.append(["X-1", "Товар", 123])
    stream = io.BytesIO()
    book.save(stream)
    response = api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload(
        filename="offers.xlsx", content_base64=base64.b64encode(stream.getvalue()).decode(),
        sheet="Offers", mapping={"article": "Code", "name": "Description", "price": "Cost"}))
    assert response.status_code == 200 and response.json()["imported"] == 1
    hit, = api.post("/trade/search", headers=headers(), json={"query": "X-1"}).json()
    assert hit["price"] == 123 and hit["sheet"] == "Offers"
    assert trade.trade_sources.read(trade.ROOT / "data" / "trade", hit["source_sha256"]) == stream.getvalue()


def test_csv_import_archives_original_under_search_digest(api):
    seed(api)
    hit, = api.post("/trade/search", headers=headers(), json={"query": "A-1"}).json()
    assert hit["source_sha256"] == hashlib.sha256(CSV).hexdigest()
    assert trade.trade_sources.read(trade.ROOT / "data" / "trade", hit["source_sha256"]) == CSV


def test_failed_import_does_not_archive(api, monkeypatch):
    def unexpected_archive(*args):
        pytest.fail("Original must not be archived when import fails")
    monkeypatch.setattr(trade.trade_sources, "archive", unexpected_archive)
    result = api.post("/trade/catalog/import", headers=headers(ADMIN),
                      json=upload(mapping={"article": "missing", "name": "name"}))
    assert result.status_code == 422


def test_archive_runs_after_import_and_failure_is_safe(api, monkeypatch):
    def failed_archive(root, data):
        assert root == trade.ROOT / "data" / "trade"
        assert data == CSV
        assert TradeCatalog(root / "catalog.sqlite").search("A-1")
        raise OSError(SECRET)
    monkeypatch.setattr(trade.trade_sources, "archive", failed_archive)
    result = api.post("/trade/catalog/import", headers=headers(ADMIN), json=upload())
    assert result.status_code == 503 and SECRET not in result.text


@pytest.mark.parametrize("exception,code", [(ValueError(SECRET), 422), (OSError(SECRET), 503)])
def test_backend_errors_do_not_leak_details(api, monkeypatch, exception, code):
    def broken(*args, **kwargs):
        raise exception
    monkeypatch.setattr(TradeCatalog, "search", broken)
    result = api.post("/trade/search", headers=headers(), json={"query": "A"})
    assert result.status_code == code and SECRET not in result.text


@pytest.mark.parametrize("field", ["price", "status", "selected", "source", "source_row", "candidates"])
def test_export_rejects_forged_offer_data_without_echoing_it(api, field):
    result = api.post("/trade/export", headers=headers(), json={
        "items": [{"query": "A-1", "qty": 1, field: SECRET}]})
    assert result.status_code == 422 and SECRET not in result.text


def export(api, **item):
    return api.post("/trade/export", headers=headers(), json={"items": [{"query": "A-1", "qty": 2, **item}]})


def test_export_selection_is_recomputed_and_id_must_match_query(api):
    seed(api)
    hit, = api.post("/trade/search", headers=headers(), json={"query": "A-1"}).json()
    assert export(api, selected_id=999).status_code == 422
    assert export(api, query="unrelated-no-match-999999", selected_id=hit["id"]).status_code == 422
    # Change the backend price after a candidate was presented to the client.
    import sqlite3
    with sqlite3.connect(trade.ROOT / "data" / "trade" / "catalog.sqlite") as db:
        db.execute("UPDATE items SET price='150' WHERE id=?", (hit["id"],))
    response = export(api, selected_id=hit["id"])
    assert response.status_code == 200
    assert response.json()["draft"] and response.json()["requires_human_review"]
    book = load_workbook(io.BytesIO(base64.b64decode(response.json()["content_base64"])))
    values = list(book.active.values)
    row = next(i for i, r in enumerate(values) if r[0] == "№")
    position = dict(zip(values[row], values[row + 1]))
    assert position["Цена за единицу"] == 150 and position["С НДС"] == 300
    assert position["Источник"] == "offers.csv"


def test_export_analog_confirmation_and_stock_are_enforced(api):
    seed(api)
    hit, = api.post("/trade/search", headers=headers(), json={"query": "OLD-2"}).json()
    assert export(api, query="OLD-2", selected_id=hit["id"]).status_code == 422
    assert export(api, query="OLD-2", selected_id=hit["id"], confirm_analog="false").status_code == 422
    response = export(api, query="OLD-2", selected_id=hit["id"], confirm_analog=True, qty=6)
    values = list(load_workbook(io.BytesIO(base64.b64decode(response.json()["content_base64"]))).active.values)
    index = next(i for i, r in enumerate(values) if r[0] == "№")
    row = dict(zip(values[index], values[index + 1]))
    assert row["Статус"] == "out_of_stock" and row["Включено в предложение"] == "Нет"
    assert next(r[7] for r in values if r[0] == "Итого предлагаемых строк — С НДС") == 0
    response = export(api, query="OLD-2", selected_id=hit["id"], confirm_analog=True)
    assert response.status_code == 200
    values = list(load_workbook(io.BytesIO(base64.b64decode(response.json()["content_base64"]))).active.values)
    index = next(i for i, r in enumerate(values) if r[0] == "№")
    row = dict(zip(values[index], values[index + 1]))
    assert row["Статус"] == "analog" and row["Включено в предложение"] == "Да"
    assert next(r[7] for r in values if r[0] == "Итого предлагаемых строк — С НДС") == 120


def test_export_respects_filters_and_no_choice_does_not_autoapprove_analog(api):
    seed(api)
    hit, = api.post("/trade/search", headers=headers(), json={"query": "A-1"}).json()
    assert api.post("/trade/export", headers=headers(), json={
        "items": [{"query": "A-1", "selected_id": hit["id"]}],
        "filters": {"brand": "Other"}}).status_code == 422
    response = export(api, query="OLD-2", confirm_analog=True)
    values = list(load_workbook(io.BytesIO(base64.b64decode(response.json()["content_base64"]))).active.values)
    index = next(i for i, r in enumerate(values) if r[0] == "№")
    assert dict(zip(values[index], values[index + 1]))["Статус"] == "review"


def test_docx_export_and_unknown_prices(api):
    seed(api)
    response = api.post("/trade/export", headers=headers(), json={
        "format": "docx", "customer": "Customer", "prices_include_vat": False,
        "items": [{"query": "C-3", "qty": 1}]})
    assert response.status_code == 200 and response.json()["filename"].endswith(".docx")
    data = base64.b64decode(response.json()["content_base64"], validate=True)
    assert ZipFile(io.BytesIO(data)).testzip() is None
    doc = Document(io.BytesIO(data))
    assert "ЧЕРНОВИК" in "\n".join(p.text for p in doc.paragraphs)
    row = {r.cells[0].text: r.cells[1].text for r in doc.tables[0].rows}
    assert row["Цена за единицу"] == "Неизвестно" and row["Включено в предложение"] == "Нет"

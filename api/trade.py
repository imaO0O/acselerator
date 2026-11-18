"""Authenticated trade router, independent of api.server.

Register after defining the authentication dependency:
    from api.trade import build_router
    app.include_router(build_router(client))

Search/select/export accept a nested ``filters`` object. Exports accept only
query, qty, selected_id and confirm_analog per item, never client offer data.
"""
import base64
import binascii
from pathlib import Path
from typing import Annotated, Literal
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from kbagent.config import ROOT, settings
from kbagent.trade_catalog import TradeCatalog
from kbagent.trade_export import export_xlsx, proposal_docx
from kbagent.trade_requests import match_batch


MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_BASE64_LENGTH = 4 * ((MAX_IMPORT_BYTES + 2) // 3)
ADMIN_ROLE = "Администратор"
QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Quantity = Annotated[float, Field(gt=0, le=1_000_000, allow_inf_nan=False, strict=True)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(_Request):
    catalog: ShortText = ""
    brand: ShortText = ""
    group: ShortText = ""
    limit: int = Field(10, ge=1, le=100, strict=True)
    available_only: bool = Field(False, strict=True)
    model: ShortText = ""
    year: int | None = Field(None, ge=1886, le=2200, strict=True)
    vin: str = Field("", max_length=17, pattern=r"^(?:[A-HJ-NPR-Za-hj-npr-z0-9]{17})?$")


class SearchRequest(_Request):
    query: QueryText
    filters: Filters = Field(default_factory=Filters)


class Item(_Request):
    query: QueryText
    qty: Quantity = 1


class SelectRequest(_Request):
    items: list[Item] = Field(min_length=1, max_length=100)
    filters: Filters = Field(default_factory=Filters)


class ImportRequest(_Request):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    content_base64: str = Field(min_length=1, max_length=MAX_BASE64_LENGTH)
    mapping: dict[QueryText, QueryText] = Field(min_length=2, max_length=500)
    catalog: QueryText = "Основной"
    sheet: QueryText | None = None


class ExportItem(Item):
    selected_id: int | None = Field(None, ge=1, strict=True)
    confirm_analog: bool = Field(False, strict=True)


class ExportRequest(_Request):
    items: list[ExportItem] = Field(min_length=1, max_length=100)
    filters: Filters = Field(default_factory=Filters)
    format: Literal["xlsx", "docx"] = "xlsx"
    customer: ShortText = ""
    prices_include_vat: bool = Field(True, strict=True)


class _SafeRoute(APIRoute):
    """Do not echo uploaded content, auth metadata or rejected offer data."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(422, detail={
                    "message": "Некорректные параметры запроса",
                    "errors": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
                }) from None

        return safe


def _catalog():
    return TradeCatalog(ROOT / "data" / "trade" / "catalog.sqlite")


def _match(items, filters):
    catalog = _catalog()

    class FilteredCatalog:
        def search(self, query):
            return catalog.search(query, **filters)

    # match_batch's first parameter is named catalog, so a catalog filter
    # cannot also be supplied as a keyword. Bind search filters on this view.
    return match_batch(FilteredCatalog(), items)


def _run(operation):
    try:
        return operation()
    except HTTPException:
        raise
    except (ValueError, BadZipFile, ParseError):
        raise HTTPException(422, "Некорректные данные каталога или параметры подбора") from None
    except Exception:
        # Parsers/SQLite may include local paths, row contents or credentials in
        # exception strings. None of those belong in an API response.
        raise HTTPException(503, "Сервис торгового каталога временно недоступен") from None


def build_router(client_dependency) -> APIRouter:
    """Inject an authenticating dependency returning a dict containing role.

    Dependency overrides work normally. Check roles on every request so changes
    to settings.tkp_roles take effect without rebuilding the router.
    """
    router = APIRouter(prefix="/trade", tags=["trade"], route_class=_SafeRoute)

    def trade_client(who: dict = Depends(client_dependency)):
        if who.get("role") not in settings.tkp_roles:
            raise HTTPException(403, "Нет доступа к торговому подбору")
        return who

    def admin_client(who: dict = Depends(client_dependency)):
        if who.get("role") != ADMIN_ROLE:
            raise HTTPException(403, "Импорт каталогов доступен только администратору")
        return who

    @router.get("/catalogs")
    def catalogs(who: dict = Depends(trade_client)):
        return _run(lambda: _catalog().options())

    @router.post("/search")
    def search(req: SearchRequest, who: dict = Depends(trade_client)):
        return _run(lambda: _catalog().search(req.query, **req.filters.model_dump()))

    @router.post("/select")
    def select(req: SelectRequest, who: dict = Depends(trade_client)):
        return _run(lambda: _match([i.model_dump() for i in req.items], req.filters.model_dump()))

    @router.post("/catalog/import")
    def import_catalog(req: ImportRequest, who: dict = Depends(admin_client)):
        # Keep filenames as source labels, never paths. Reject both separators
        # regardless of the host platform used for deployment.
        if ("/" in req.filename or "\\" in req.filename or ":" in req.filename
                or any(ord(c) < 32 for c in req.filename)
                or Path(req.filename).suffix.lower() not in {".xlsx", ".csv"}):
            raise HTTPException(422, "Нужно имя файла .xlsx или .csv без пути")
        try:
            data = base64.b64decode(req.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "content_base64 — не base64") from None
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "Каталог — до 100 МБ")
        if not data:
            raise HTTPException(422, "Файл пуст")
        return _run(lambda: _catalog().import_table(req.filename, data, req.mapping,
                                                   catalog=req.catalog, sheet=req.sheet))

    @router.post("/export")
    def export(req: ExportRequest, who: dict = Depends(trade_client)):
        def generate():
            positions = _match([
                {"query": i.query, "qty": i.qty} for i in req.items
            ], req.filters.model_dump())
            for item, position in zip(req.items, positions):
                if item.selected_id is None:
                    continue
                candidate = next((c for c in position["candidates"]
                                  if c["id"] == item.selected_id), None)
                if candidate is None:
                    raise HTTPException(422, "Выбранная позиция отсутствует в текущих результатах поиска")
                exact = candidate["match_type"] == "exact" and not candidate.get("needs_review")
                if not exact and not item.confirm_analog:
                    raise HTTPException(422, "Подтвердите выбор аналога: confirm_analog=true")
                position["selected"] = candidate
                position["status"] = "exact" if exact else "analog"
                if candidate.get("stock") is not None and candidate["stock"] < item.qty:
                    position["status"] = "out_of_stock"
            exporter = export_xlsx if req.format == "xlsx" else proposal_docx
            data = exporter(positions, customer=req.customer, prices_include_vat=req.prices_include_vat)
            mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if req.format == "xlsx"
                    else "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            return {"filename": f"trade-proposal.{req.format}", "media_type": mime,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                    "draft": True, "requires_human_review": True}

        return _run(generate)

    return router

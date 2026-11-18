"""Structured supplier catalog, independent of application settings.

Mappings are canonical field -> source header (the inverse is also accepted).
Required fields: article, name. Optional: brand, group, price, stock, lead_time,
vat_rate, cross_refs (semicolon/pipe/newline separated explicit analog articles),
mark, model, year/year_from/year_to, vin, applicability, price_level.
``price:<customer level>`` and ``discount:<customer level>`` map additional prices
and percentage discounts. ``quantity`` is an alias for stock. Prices are stored
as decimal strings; only the public search response converts them to floats.

Imports replace the named catalog atomically, retaining other catalogs. Any
invalid row rejects the entire import with source row diagnostics.
Search never creates a database and uses a separate mode=ro connection. Only
unique exact article hits can avoid review; every cross reference needs review.
Descriptions are candidates, never evidence of vehicle suitability. VINs are
opaque explicit identifiers: there is deliberately no VIN decoder here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from difflib import SequenceMatcher
from itertools import islice
from pathlib import Path


_CONFUSABLES = str.maketrans("АВЕКМНОРСТХУІ", "ABEKMHOPCTXYI")
_FIELDS = {
    "article", "name", "brand", "group", "price", "stock", "lead_time",
    "vat_rate", "cross_refs", "mark", "model", "year", "year_from", "year_to",
    "vin", "applicability", "price_level",
}
_ALIASES = {"quantity": "stock", "vat": "vat_rate", "cross": "cross_refs", "cross_numbers": "cross_refs",
            "analogs": "cross_refs", "manufacturer": "brand"}
_MAX_BYTES = 100 * 1024 * 1024
_MAX_ROWS = 500_000
_MAX_CELL = 10_000
_MAX_COLUMNS = 500
_MAX_ERRORS = 100


class CatalogValidationError(ValueError):
    """Rejected import; ``report`` contains bounded row diagnostics, no changes."""
    def __init__(self, report: dict):
        self.report = {**report, "validated": report["imported"], "imported": 0, "committed": False}
        diagnostics = "; ".join(f"row {e['source_row']}: {e['error']}" for e in report["errors"])
        super().__init__(f"Catalog unchanged: {report['rejected']} invalid rows. {diagnostics}")


def normalize_article(value: object) -> str:
    """Fold only visual Cyrillic/Latin lookalikes, case, whitespace and dashes.

    Other punctuation remains significant (AB/12 is not AB12).
    """
    text = unicodedata.normalize("NFKC", str(value)).upper().translate(_CONFUSABLES)
    return "".join(c for c in text if not c.isspace() and c != "-"
                   and unicodedata.category(c) != "Pd")


def _text(value: object) -> str:
    if isinstance(value, (float, Decimal)):
        return format(Decimal(str(value)), "f")
    return "" if value is None else str(value).strip()


def _norm(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().split())


def decimal_value(value: object) -> Decimal | None:
    """Parse a nonnegative finite decimal, including Russian decimal commas.

    Blank -> None; invalid, negative, mixed decimal separators or currency/unit
    text -> ValueError. Spaces may group thousands only (not arbitrary digits).
    Bounds keep both SQLite availability checks and float output finite.
    """
    text = _text(value)
    if not text:
        return None
    if isinstance(value, bool):
        raise ValueError("expected a decimal number")
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    if not re.fullmatch(r"\+?(?:\d+|\d{1,3}(?: \d{3})+)(?:[.,]\d+)?", text):
        raise ValueError(f"invalid decimal: {text[:80]}")
    try:
        result = Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or result < 0 or result > Decimal("1e15"):
        raise ValueError("decimal outside 0..10^15")
    if result.as_tuple().exponent < -12:
        raise ValueError("decimal has more than 12 fractional digits")
    return result


def _decimal_text(value: object) -> str | None:
    number = decimal_value(value)
    return format(number, "f") if number is not None else None


def _percent(value: object) -> Decimal | None:
    text = _text(value).removesuffix("%").strip()
    number = decimal_value(text)
    if number is not None and number > 100:
        raise ValueError("percentage outside 0..100")
    return number


def _year(value: object) -> int | None:
    text = _text(value)
    if not text:
        return None
    if not re.fullmatch(r"\d{4}(?:\.0)?", text):
        raise ValueError("year must be a four-digit integer")
    result = int(float(text))
    if not 1886 <= result <= 2200:
        raise ValueError("year outside 1886..2200")
    return result


def _parts(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;|\n]+", value) if p.strip()]


def _delimiter(text: str, suffix: str) -> str:
    """Inspect logical records, so quoted multiline cells cannot confuse sniffing.

    A valid separator splits the header and keeps subsequent row widths stable.
    Prefer a stable multi-column parse over a one-column parse; break remaining
    ties using the file type. At most 21 logical rows are inspected per choice.
    """
    choices = []
    preferred = "\t" if suffix == ".tsv" else ","
    for delimiter in (",", ";", "\t"):
        try:
            sample = list(islice(csv.reader(io.StringIO(text, newline=""),
                                           delimiter=delimiter, strict=True), 21))
        except csv.Error:
            continue
        if not sample:
            continue
        width = len(sample[0])
        body = [row for row in sample[1:] if any(_text(v) for v in row)]
        stability = sum(len(row) == width for row in body) / len(body) if body else 1
        choices.append(((width > 1, stability, width, delimiter == preferred), delimiter))
    return max(choices)[1] if choices else preferred


@contextmanager
def _table(filename: str, data: bytes, sheet: str | None):
    if not isinstance(data, bytes) or len(data) > _MAX_BYTES:
        raise ValueError("table must be bytes, at most 100 MiB")
    suffix = Path(filename).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        if sheet:
            raise ValueError("CSV has no sheets")
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV encoding must be UTF-8 or Windows-1251")
        rows = iter(csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text, suffix), strict=True))
        try:
            yield rows, [], ""
        except csv.Error as exc:
            raise ValueError(f"invalid CSV: {exc}") from exc
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        # Formula text must be rejected in mapped cells; never trust stale caches.
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            sheets = book.sheetnames
            selected = sheet if sheet is not None else sheets[0]
            if selected not in sheets:
                raise ValueError(f"unknown sheet: {selected}")
            if (book[selected].max_column or 0) > _MAX_COLUMNS or (book[selected].max_row or 0) > _MAX_ROWS + 1:
                raise ValueError("worksheet exceeds row or column limits")
            yield book[selected].iter_rows(values_only=True), sheets, selected
        finally:
            book.close()
    else:
        raise ValueError("supported formats: .csv, .tsv, .xlsx")


def _headers(rows) -> list[str]:
    columns = [_text(v) for v in next(rows, ())]
    while columns and not columns[-1]:
        columns.pop()
    if not columns or len(columns) > _MAX_COLUMNS:
        raise ValueError("missing headers or too many columns")
    if any(not c for c in columns) or len(set(columns)) != len(columns):
        raise ValueError("column headers must be nonempty and unique")
    return columns


def preview_table(filename: str, data: bytes, sheet: str | None = None) -> dict:
    """Preview a source table without requiring a database path."""
    with _table(filename, data, sheet) as (rows, sheets, _):
        columns = _headers(rows)
        preview = [dict(zip(columns, [_text(row[i]) if i < len(row) else ""
                                     for i in range(len(columns))]))
                   for row in islice(rows, 20)]
    return {"columns": columns, "rows": preview, "sheets": sheets}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, sheet TEXT NOT NULL,
 sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, mapping TEXT NOT NULL,
 catalog TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, article TEXT NOT NULL, article_norm TEXT NOT NULL,
 article_reverse TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
 brand TEXT NOT NULL, brand_norm TEXT NOT NULL, group_name TEXT NOT NULL,
 group_norm TEXT NOT NULL, catalog TEXT NOT NULL, catalog_norm TEXT NOT NULL,
 price TEXT, stock TEXT, lead_time TEXT NOT NULL, vat_rate TEXT,
 applicability TEXT NOT NULL, year_from INTEGER, year_to INTEGER,
 price_level TEXT NOT NULL, price_levels TEXT NOT NULL, discounts TEXT NOT NULL,
 import_id INTEGER NOT NULL REFERENCES imports(id), source_row INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_article_idx ON items(article_norm);
CREATE INDEX IF NOT EXISTS items_level_idx ON items(article_norm,catalog_norm,brand_norm,price_level);
CREATE INDEX IF NOT EXISTS items_reverse_idx ON items(article_reverse);
CREATE INDEX IF NOT EXISTS items_catalog_idx ON items(catalog_norm);
CREATE INDEX IF NOT EXISTS items_brand_idx ON items(brand_norm);
CREATE INDEX IF NOT EXISTS items_group_idx ON items(group_norm);
CREATE TABLE IF NOT EXISTS cross_refs (
 article_norm TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
 original TEXT NOT NULL, PRIMARY KEY(article_norm, item_id)
);
CREATE INDEX IF NOT EXISTS cross_item_idx ON cross_refs(item_id, article_norm);
CREATE TABLE IF NOT EXISTS fitment (
 kind TEXT NOT NULL, value TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
 PRIMARY KEY(kind, value, item_id)
);
CREATE INDEX IF NOT EXISTS fitment_item_idx ON fitment(item_id,kind,value);
CREATE TABLE IF NOT EXISTS name_terms (
 term TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
 PRIMARY KEY(term,item_id)
);
"""


class TradeCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _reader(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def preview_table(self, filename: str, data: bytes, sheet: str | None = None) -> dict:
        """Return headers and at most 20 preview dictionaries; no database writes."""
        return preview_table(filename, data, sheet)

    @staticmethod
    def _mapping(mapping: dict[str, str], columns: list[str]) -> dict[str, int]:
        if not isinstance(mapping, dict) or not mapping or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise ValueError("mapping must be a nonempty string-to-string dictionary")
        result = {}
        for key, column in mapping.items():
            # Accommodate column -> canonical mappings from table mapping UIs.
            key_is_field = key in _FIELDS or key in _ALIASES or key.startswith(("price:", "discount:", "price_level:"))
            column_is_field = column in _FIELDS or column in _ALIASES or column.startswith(("price:", "discount:", "price_level:"))
            if not key_is_field and key in columns and column_is_field:
                key, column = column, key
            key = _ALIASES.get(key, key)
            if key.startswith("price_level:"):
                key = "price:" + key.split(":", 1)[1]
            if key not in _FIELDS and not key.startswith(("price:", "discount:")):
                raise ValueError(f"unknown mapping field: {key}")
            if key in result or column not in columns:
                raise ValueError(f"duplicate field or missing column: {key}: {column}")
            if ":" in key and not _norm(key.split(":", 1)[1]):
                raise ValueError("empty customer price level")
            result[key] = columns.index(column)
        if not {"article", "name"} <= result.keys():
            raise ValueError("mapping requires article and name")
        return result

    def import_table(self, filename: str, data: bytes, mapping: dict[str, str],
                     catalog: str = "Основной", sheet: str | None = None) -> dict:
        """Replace this catalog and return committed/imported/replaced counts.

        Errors include physical source rows (header is row 1), bounded to 100
        diagnostics. Any invalid row or file raises ValueError and rolls back.
        """
        catalog = _text(catalog)
        if not catalog or len(catalog) > 200:
            raise ValueError("catalog must contain 1..200 characters")
        report = {"imported": 0, "rejected": 0, "errors": [], "rows": 0}
        with _table(filename, data, sheet) as (rows, _, selected):
            columns = _headers(rows)
            mapped = self._mapping(mapping, columns)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SCHEMA)
                with db:
                    stamp = datetime.now(timezone.utc).isoformat()
                    source = filename.replace("\\", "/").rsplit("/", 1)[-1]
                    import_id = db.execute(
                        "INSERT INTO imports(source,sheet,sha256,imported_at,mapping,catalog) "
                        "VALUES(?,?,?,?,?,?)", (source, selected, hashlib.sha256(data).hexdigest(),
                                               stamp, json.dumps(mapping, ensure_ascii=False), catalog)
                    ).lastrowid
                    for source_row, raw in enumerate(rows, 2):
                        if source_row > _MAX_ROWS + 1:
                            raise ValueError("table exceeds 500000 rows")
                        if not any(_text(v) for v in raw):
                            continue
                        report["rows"] += 1
                        try:
                            if len(raw) > len(columns) and any(_text(v) for v in raw[len(columns):]):
                                raise ValueError("row contains cells beyond headers")
                            values = {key: _text(raw[i]) if i < len(raw) else ""
                                      for key, i in mapped.items()}
                            if any(len(v) > _MAX_CELL or v.startswith("=") for v in values.values()):
                                raise ValueError("formulas or cells over 10000 characters are not accepted")
                            prepared, crosses, fitment = self._prepare(values, catalog, import_id,
                                                                      source_row, stamp)
                        except ValueError as exc:
                            report["rejected"] += 1
                            if len(report["errors"]) < _MAX_ERRORS:
                                report["errors"].append({"source_row": source_row, "error": str(exc)})
                            continue
                        item_id = db.execute(
                            "INSERT INTO items(article,article_norm,article_reverse,name,name_norm,"
                            "brand,brand_norm,group_name,group_norm,catalog,catalog_norm,price,stock,"
                            "lead_time,vat_rate,applicability,year_from,year_to,price_level,price_levels,"
                            "discounts,import_id,source_row,created_at,updated_at) VALUES(" +
                            ",".join("?" for _ in prepared) + ")", prepared).lastrowid
                        db.executemany("INSERT OR IGNORE INTO cross_refs VALUES(?,?,?)",
                                       ((normalize_article(c), item_id, c) for c in crosses))
                        db.executemany("INSERT OR IGNORE INTO fitment VALUES(?,?,?)",
                                       ((kind, val, item_id) for kind, val in fitment))
                        db.executemany("INSERT OR IGNORE INTO name_terms VALUES(?,?)",
                                       ((term, item_id) for term in set(re.findall(r"\w+", values["name"].casefold()))))
                        report["imported"] += 1
                    if report["rejected"]:
                        raise CatalogValidationError(report)
                    if not report["imported"]:
                        raise ValueError("Catalog unchanged: source has no data rows")
                    old = "SELECT id FROM items WHERE catalog_norm=? AND import_id<>?"
                    old_params = (_norm(catalog), import_id)
                    replaced = db.execute("SELECT COUNT(*) FROM items WHERE catalog_norm=? AND import_id<>?",
                                          old_params).fetchone()[0]
                    for child in ("cross_refs", "fitment", "name_terms"):
                        db.execute(f"DELETE FROM {child} WHERE item_id IN ({old})", old_params)
                    db.execute("DELETE FROM items WHERE catalog_norm=? AND import_id<>?", old_params)
                    report.update(import_id=import_id, imported_at=stamp, source=source,
                                  sheet=selected, replaced=replaced, committed=True)
        return report

    @staticmethod
    def _prepare(v, catalog, import_id, source_row, stamp):
        article, name = v["article"], v["name"]
        norm = normalize_article(article)
        if not norm or not name or len(article) > 200 or len(name) > 2000:
            raise ValueError("article and name are required (maximum 200/2000 characters)")
        price, stock = _decimal_text(v.get("price")), _decimal_text(v.get("stock"))
        vat = _percent(v.get("vat_rate"))
        year = _year(v.get("year"))
        start, end = _year(v.get("year_from")), _year(v.get("year_to"))
        if year is not None:
            if (start is not None and start != year) or (end is not None and end != year):
                raise ValueError("year conflicts with year range")
            start = end = year
        if start is not None and end is not None and start > end:
            raise ValueError("year_from exceeds year_to")
        levels, discounts = {}, {}
        for key, value in v.items():
            if key.startswith(("price:", "discount:")):
                kind, level = key.split(":", 1)
                target = levels if kind == "price" else discounts
                level = _norm(level)
                if level in target:
                    raise ValueError("duplicate normalized customer level")
                number = decimal_value(value) if kind == "price" else _percent(value)
                target[level] = str(number) if number is not None else None
        fitment = []
        for kind in ("mark", "model", "vin"):
            for part in _parts(v.get(kind, "")):
                value = _norm(part) if kind != "vin" else part.upper()
                if kind == "vin" and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value):
                    raise ValueError("VIN must be an explicit 17-character VIN")
                fitment.append((kind, value))
        # A combined mark/model is explicit only for single mark + single model.
        marks, models = _parts(v.get("mark", "")), _parts(v.get("model", ""))
        if len(marks) == len(models) == 1:
            fitment.append(("model", _norm(marks[0] + " " + models[0])))
        applicability = v.get("applicability", "")
        explicit = [f"{key}: {v[key]}" for key in ("mark", "model", "year", "year_from", "year_to", "vin")
                    if v.get(key)]
        applicability = "; ".join(filter(None, [applicability, *explicit]))
        brand, group = v.get("brand", ""), v.get("group", "")
        prepared = (article, norm, norm[::-1], name, _norm(name), brand, _norm(brand),
                    group, _norm(group), catalog, _norm(catalog), price, stock,
                    v.get("lead_time", ""), str(vat) if vat is not None else None,
                    applicability, start, end, _norm(v.get("price_level", "")),
                    json.dumps(levels), json.dumps(discounts), import_id, source_row, stamp, stamp)
        return prepared, _parts(v.get("cross_refs", "")), fitment

    def options(self) -> dict:
        if not self.path.exists():
            return {"catalogs": [], "brands": [], "groups": []}
        with closing(self._reader()) as db:
            return {key: [r[0] for r in db.execute(
                f"SELECT DISTINCT {column} FROM items WHERE {column} <> '' ORDER BY {column}")]
                    for key, column in (("catalogs", "catalog"), ("brands", "brand"), ("groups", "group_name"))}

    def search(self, query: str, *, catalog: str = "", brand: str = "", group: str = "",
               limit: int = 10, available_only: bool = False, model: str = "",
               year=None, vin: str = "", price_level: str = "", discount=0) -> list[dict]:
        """Exact plus explicit cross, then reviewed description/typo candidates.

        Limits are capped at 100. Typo pools are bounded to 2000 indexed prefix /
        suffix candidates, so suggestions are intentionally not exhaustive.
        Customer discount is a percentage, applied after an explicit level
        discount. Missing requested prices stay None and require review.
        ``model`` accepts an exact model, mark, or explicit 'mark model' pair.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 100)
        query = _text(query)
        if len(query) > 200:
            raise ValueError("query exceeds 200 characters")
        reduction = _percent(discount)
        if reduction is None:
            raise ValueError("discount must be a percentage")
        requested_year = _year(year)
        vin = _text(vin).upper()
        if vin and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            raise ValueError("VIN must be an explicit 17-character VIN")
        if not query or not normalize_article(query) or not self.path.exists():
            return []
        filters, params = [], []
        for column, value in (("catalog_norm", catalog), ("brand_norm", brand), ("group_norm", group)):
            if value:
                filters.append(f"i.{column} = ?")
                params.append(_norm(value))
        if available_only:
            filters.append("CAST(i.stock AS REAL) > 0")
        if model:
            filters.append("EXISTS (SELECT 1 FROM fitment f WHERE f.item_id=i.id "
                           "AND f.kind IN ('model','mark') AND f.value=?)")
            params.append(_norm(model))
        if vin:
            filters.append("EXISTS (SELECT 1 FROM fitment f WHERE f.item_id=i.id AND f.kind='vin' AND f.value=?)")
            params.append(vin)
        if requested_year is not None:
            filters.extend(["(i.year_from IS NOT NULL OR i.year_to IS NOT NULL)",
                            "(i.year_from IS NULL OR i.year_from<=?)",
                            "(i.year_to IS NULL OR i.year_to>=?)"])
            params.extend((requested_year, requested_year))
        # Explicit customer rows override that product's base offer. Other
        # customers' rows never appear as suggestions or quoted base prices.
        level = _norm(price_level)
        if level:
            filters.append("(i.price_level=? OR (i.price_level='' AND NOT EXISTS ("
                           "SELECT 1 FROM items tier WHERE tier.article_norm=i.article_norm "
                           "AND tier.catalog_norm=i.catalog_norm AND tier.brand_norm=i.brand_norm "
                           "AND tier.price_level=?)))")
            params.extend((level, level))
        else:
            filters.append("i.price_level=''")
        where = " AND ".join(filters)
        norm = normalize_article(query)
        with closing(self._reader()) as db:
            def fetch(condition, args, size=limit + 1):
                return db.execute(
                    "SELECT i.*, p.source,p.sheet,p.sha256,p.imported_at FROM items i "
                    "JOIN imports p ON p.id=i.import_id WHERE " + condition + " AND " + where +
                    " ORDER BY i.id LIMIT ?", [*args, *params, size]).fetchall()

            hits = fetch("i.article_norm=?", [norm])
            # Batch requests may concatenate the article and product name. Look
            # up bounded contiguous token spans through the same exact index;
            # never label a fuzzy token as an exact article.
            candidates = [norm]
            if not hits:
                tokens = query.split()[:24]
                candidates = sorted({normalize_article(" ".join(tokens[start:end]))
                                     for start in range(len(tokens))
                                     for end in range(start + 1, min(start + 6, len(tokens)) + 1)})
                candidates = [candidate for candidate in candidates if candidate]
                placeholders = ",".join("?" for _ in candidates)
                hits = fetch(f"i.article_norm IN ({placeholders})", candidates)
            placeholders = ",".join("?" for _ in candidates)
            # Explicit edges work in either direction, but never transitively.
            # Include outgoing references even when the exact offer is unavailable.
            cross_condition = (
                "i.id IN (SELECT item_id FROM cross_refs WHERE article_norm IN (" + placeholders + ") "
                "UNION SELECT target.id FROM items original JOIN cross_refs c ON c.item_id=original.id "
                "JOIN items target ON target.article_norm=c.article_norm "
                "WHERE original.article_norm IN (" + placeholders + ") "
                "AND (original.price_level='' OR original.price_level=?))")
            crosses = fetch(cross_condition, [*candidates, *candidates, _norm(price_level)])
            if hits or crosses:
                exact_ids = {row["id"] for row in hits}
                results = [self._result(row, "exact", len(hits) > 1, _norm(price_level), reduction)
                           for row in hits[:limit]]
                results.extend(self._result(row, "cross", True, _norm(price_level), reduction)
                               for row in crosses if row["id"] not in exact_ids)
                return results[:limit]
            if not hits:
                terms = list(dict.fromkeys(re.findall(r"\w+", _norm(query))))[:12]
                if terms:
                    # Intersect explicit description words using the term index.
                    condition = " AND ".join(
                        "i.id IN (SELECT item_id FROM name_terms WHERE term=?)" for _ in terms)
                    hits = fetch(condition, terms)
                match_type = "description"
            if not hits and len(norm) >= 3:
                pool = {}
                for column, prefix in (("article_norm", norm[:2]), ("article_reverse", norm[::-1][:2])):
                    # Range comparisons, rather than LIKE, retain B-tree index use.
                    for row in fetch(f"i.{column}>=? AND i.{column}<?",
                                     [prefix, prefix + "\U0010ffff"], 1000):
                        pool[row["id"]] = row
                scored = [(SequenceMatcher(None, norm, row["article_norm"]).ratio(), row)
                          for row in pool.values() if abs(len(row["article_norm"]) - len(norm)) <= 2]
                hits = [row for score, row in sorted(scored, key=lambda p: (-p[0], p[1]["id"]))
                        if score >= .78][:limit + 1]
                match_type = "fuzzy"
            ambiguous = len(hits) > 1
            return [self._result(row, match_type, ambiguous, _norm(price_level), reduction)
                    for row in hits[:limit]]

    @staticmethod
    def _result(row, match_type, ambiguous, level, reduction):
        levels, discounts = json.loads(row["price_levels"]), json.loads(row["discounts"])
        price = row["price"]
        if level and row["price_level"] != level:
            # An explicitly supplied level discount can use the supplied base price.
            price = levels.get(level) if level in levels else (price if discounts.get(level) is not None else None)
        number = Decimal(price) if price is not None else None
        if number is not None:
            level_discount = Decimal(discounts.get(level) or "0") if level else Decimal(0)
            with localcontext() as ctx:
                ctx.prec = 64
                number = (number * (1 - level_discount / 100) * (1 - reduction / 100)).quantize(
                    Decimal(".01"), rounding=ROUND_HALF_UP)
        return {
            "id": row["id"], "article": row["article"], "name": row["name"],
            "brand": row["brand"], "group": row["group_name"], "catalog": row["catalog"],
            "price": float(number) if number is not None else None,
            "stock": float(row["stock"]) if row["stock"] is not None else None,
            "lead_time": row["lead_time"],
            "vat_rate": float(row["vat_rate"]) if row["vat_rate"] is not None else None,
            "source": row["source"], "source_row": row["source_row"],
            "match_type": match_type,
            "needs_review": ambiguous or match_type != "exact" or number is None,
            "applicability": row["applicability"], "sheet": row["sheet"],
            "source_sha256": row["sha256"], "imported_at": row["imported_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "price_level": level, "discount": float(reduction),
        }

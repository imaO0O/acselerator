"""Synthetic ДЕМО fixtures and offline evaluation of the real trade APIs.

No models, network calls, OCR or geometric drawing matching are used. Metrics
are fractions with explicit numerators/denominators; an absent category is None.
Each evaluation imports fixtures into a fresh temporary SQLite database.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .trade_catalog import TradeCatalog
from .trade_requests import match_batch, read_file


DEMO_ROOT = Path(__file__).resolve().parents[1] / "data" / "trade_demo"
CASES = ("agri", "auto", "drawings")
CATALOG_MAPPING = {field: field for field in (
    "article", "name", "brand", "group", "price", "stock", "lead_time",
    "vat_rate", "cross_refs", "applicability",
)}
METRICS = (
    "exact_accuracy", "cross_candidate_coverage", "description_candidate_coverage",
    "drawing_text_article_accuracy", "no_data_accuracy",
)


def _metric(hits, total):
    return {"correct": hits, "total": total, "value": hits / total if total else None}


def _check_rows(actual, expected, case, source):
    """Score independent expected positions, including missing/extra outputs."""
    details = []
    for index in range(max(len(actual), len(expected))):
        result = actual[index] if index < len(actual) else None
        target = expected[index] if index < len(expected) else None
        selected = (result or {}).get("selected")
        candidates = (result or {}).get("candidates", [])
        category = target["kind"] if target else "unexpected"
        wanted = set((target or {}).get("articles", []))
        found = {c["article"] for c in candidates if c["match_type"] == category}
        if category == "exact":
            success = (selected is not None and selected["article"] in wanted
                       and selected["match_type"] == "exact" and result["status"] == "exact")
        elif category in ("cross", "description"):
            success = bool(wanted) and wanted <= found and selected is None and result["status"] == "review"
        elif category == "unknown":
            success = result is not None and not candidates and selected is None and result["status"] == "not_found"
        else:
            success = False
        quantity_ok = result is not None and target is not None and result["qty"] == target["qty"]
        metric = {"exact": "exact_accuracy", "cross": "cross_candidate_coverage",
                  "description": "description_candidate_coverage", "unknown": "no_data_accuracy"}.get(category)
        # Coverage counts expected articles individually, rather than treating any
        # plausible candidate as a success. Status/selection/qty remain hard checks.
        coverage = len(wanted & found) if category in ("cross", "description") else int(success)
        denominator = len(wanted) if category in ("cross", "description") else 1
        details.append({
            "case": case, "source": source, "line": index + 1,
            "kind": category, "query": (result or {}).get("query"),
            "expected_articles": sorted(wanted),
            "selected_article": selected["article"] if selected else None,
            "candidates": [{"article": c["article"], "match_type": c["match_type"]} for c in candidates],
            "status": (result or {}).get("status"), "quantity_ok": quantity_ok,
            "passed": bool(success and quantity_ok), "metric": metric,
            "correct": coverage, "total": denominator,
        })
    return details


def evaluate(root: Path | str = DEMO_ROOT) -> dict:
    """Evaluate all three cases; errors propagate instead of becoming successes.

    Candidate coverage means expected candidate recall, not confirmed fitment or
    precision. Crosses are explicit synthetic supplier statements only. Drawing
    article accuracy checks the extracted designation against a selected exact
    article; fields and the specification are checked separately.
    """
    root = Path(root)
    checks, imports = [], []
    with TemporaryDirectory(prefix="trade-demo-") as scratch:
        for name in CASES:
            folder = root / name
            expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
            if expected.get("demo") is not True or expected.get("case") != name:
                raise ValueError(f"{name}: expected.json must identify the ДЕМО case")
            catalog = TradeCatalog(Path(scratch) / f"{name}.sqlite")
            report = catalog.import_table("catalog.csv", (folder / "catalog.csv").read_bytes(),
                                          CATALOG_MAPPING, catalog=f"ДЕМО / {name}")
            imports.append({"case": name, "imported": report["imported"], "rejected": report["rejected"]})
            checks.append({"case": name, "source": "catalog.csv", "kind": "import",
                           "passed": report["imported"] == expected["catalog_rows"] and report["rejected"] == 0})
            request = read_file("request.csv", (folder / "request.csv").read_bytes())
            checks.extend(_check_rows(match_batch(catalog, request["items"]), expected["positions"], name, "request.csv"))
            if "drawing" in expected:
                drawing = expected["drawing"]
                parsed = read_file("drawing.pdf", (folder / "drawing.pdf").read_bytes())
                checks.append({"case": name, "source": "drawing.pdf", "kind": "drawing_fields",
                               "actual": parsed["fields"], "expected": drawing["fields"],
                               "passed": parsed["fields"] == drawing["fields"]})
                # The designation is a separate query from the specification.
                designation = parsed["fields"].get("designation", "")
                matched = match_batch(catalog, [{"query": designation, "qty": 1}]) if designation else []
                selected = matched[0]["selected"] if matched else None
                success = (selected is not None and selected["article"] == drawing["article"]
                           and selected["match_type"] == "exact" and matched[0]["status"] == "exact")
                checks.append({"case": name, "source": "drawing.pdf", "kind": "drawing_article",
                               "metric": "drawing_text_article_accuracy", "correct": int(success), "total": 1,
                               "selected_article": selected["article"] if selected else None, "passed": success})
                checks.extend(_check_rows(match_batch(catalog, parsed["items"]), drawing["positions"], name, "drawing.pdf"))
    metrics = {}
    for metric in METRICS:
        relevant = [check for check in checks if check.get("metric") == metric]
        metrics[metric] = _metric(sum(c["correct"] for c in relevant), sum(c["total"] for c in relevant))
    return {"demo": True, "label": "ДЕМО: синтетические данные, не доказательство реальной применимости",
            "offline": True, "cases": list(CASES), "imports": imports, "metrics": metrics,
            "passed": all(check["passed"] for check in checks),
            "failed_checks": sum(not check["passed"] for check in checks), "checks": checks}


def generate_drawing(folder: Path | str = DEMO_ROOT / "drawings") -> Path:
    """Rebuild the original textual PDF from drawing.txt with PyMuPDF.

    The bundled Droid Sans Fallback font supports Cyrillic; no CAD or external
    drawing is used. The document deliberately has no geometric representation.
    """
    import pymupdf

    folder = Path(folder)
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_font(fontname="demo", fontbuffer=pymupdf.Font("cjk").buffer)
        page.draw_rect(pymupdf.Rect(28, 28, 567, 814), color=(0.3, 0.4, 0.5))
        for index, line in enumerate((folder / "drawing.txt").read_text(encoding="utf-8").splitlines()):
            page.insert_text((44, 60 + index * 30), line, fontname="demo", fontsize=12)
        document.set_metadata({"title": "ДЕМО - синтетический текстовый чертеж",
                               "author": "", "subject": "CC0 synthetic content; no geometry matching"})
        document.subset_fonts()
        path = folder / "drawing.pdf"
        document.save(path, garbage=4, deflate=True)
    return path

"""Скачивает открытые документы из data/sources.yaml в data/raw/external.

Для каждого документа перебираются зеркала, пока не найдётся рабочее.
Файл принимается, только если это действительно PDF.

    python scripts/fetch_docs.py
"""
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "external"
UA = {"User-Agent": "Mozilla/5.0"}


def download(url: str) -> bytes:
    parts = urllib.parse.urlsplit(url)
    # кириллица и пробелы в пути
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path)))
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    failed = []
    for doc in sources["documents"]:
        target = OUT / f"{doc['id']}.pdf"
        if target.exists():
            print(f"есть      {target.name}")
            continue
        for url in doc["urls"]:
            try:
                data = download(url)
            except Exception as e:
                print(f"ошибка    {doc['id']}: {url} — {e}")
                continue
            if not data.startswith(b"%PDF"):
                print(f"не PDF    {doc['id']}: {url}")
                continue
            target.write_bytes(data)
            print(f"скачан    {target.name} ({len(data) / 1e6:.1f} МБ)")
            break
        else:
            failed.append(doc["id"])
    if failed:
        print("Не удалось скачать:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

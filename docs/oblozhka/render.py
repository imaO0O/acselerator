"""Обложка проекта: HTML → PNG.

    python docs/oblozhka/render.py                         # oblozhka.html → oblozhka.png, чёткость ×2 (3840×2160)
    python docs/oblozhka/render.py oblozhka_figma.html 1   # фон из Figma 1920×1080 — без увеличения, чтобы не размыть

Шрифты (Unbounded, Golos Text) подгружаются с Google Fonts — при сборке нужен интернет.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
html = HERE / (sys.argv[1] if len(sys.argv) > 1 else "oblozhka.html")
scale = float(sys.argv[2]) if len(sys.argv) > 2 else 2
out = html.with_suffix(".png")

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=scale)
    page.goto(html.as_uri(), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(800)
    loaded = page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
    page.screenshot(path=str(out))
    browser.close()
print(out, "шрифты:", sorted(set(loaded)))

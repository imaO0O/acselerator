"""Обложка проекта: oblozhka.html → oblozhka.png (1920×1080, с удвоенной чёткостью — 3840×2160).
Шрифты Unbounded и Golos Text подгружаются с Google Fonts — при сборке нужен интернет.

    python docs/oblozhka/render.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=2)
    page.goto((HERE / "oblozhka.html").as_uri(), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(800)
    page.screenshot(path=str(HERE / "oblozhka.png"))
    browser.close()
print(HERE / "oblozhka.png")

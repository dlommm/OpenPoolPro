"""Capture a real app screenshot per theme for the Appearance picker.

Run against a live instance (the local dev app):
    APP_URL=http://localhost:8000 APP_PASSWORD=... .venv/bin/python scripts/theme_screenshots.py [/page]

Writes app/static/themes/<theme>.png (desktop view). Flips the global active_theme
while capturing, then restores whatever it was before.
"""
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
PASSWORD = os.environ["APP_PASSWORD"]
THEMES = ["warm-coastal", "control-room", "fresh-field"]
SHOT_PAGE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHOT_PAGE", "/")
OUT = pathlib.Path("app/static/themes")
OUT.mkdir(parents=True, exist_ok=True)


def set_theme(page, theme):
    page.request.post(f"{BASE}/settings/appearance", form={"active_theme": theme})


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1200, "height": 780}, device_scale_factor=1)
    page = ctx.new_page()

    page.goto(f"{BASE}/login")
    page.fill("input[name=username]", "owner")
    page.fill("input[name=password]", PASSWORD)
    page.click("form button")
    page.wait_for_load_state("networkidle")

    orig = "warm-coastal"
    try:
        page.goto(f"{BASE}/settings")
        orig = page.eval_on_selector("input[name=active_theme]:checked", "el => el.value")
    except Exception:
        pass

    for theme in THEMES:
        set_theme(page, theme)
        page.goto(f"{BASE}{SHOT_PAGE}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / f"{theme}.png"),
                        clip={"x": 0, "y": 0, "width": 1200, "height": 740})
        print("shot", theme)

    set_theme(page, orig)
    browser.close()

print("done ->", OUT)

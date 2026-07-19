"""Visual smoke check: proves the design system actually renders on every screen.

Catches the failure class where markup ships but the stylesheet is stale or
broken (naked-HTML pages). Run against a live instance:

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py [shots_dir]

Requires: pip install playwright && playwright install chromium
Exit code 0 = all screens styled; non-zero = a screen failed a check.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("APP_URL", "http://localhost:8000")
PASSWORD = os.environ.get("APP_PASSWORD", "coastline-demo")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None

SCREENS = ["/", "/week", "/customers", "/jobs", "/money", "/settings"]

# Computed-style assertions: proves app.css APPLIED, not merely returned 200.
CHECKS = """
() => {
  const body = getComputedStyle(document.body);
  const probs = [];
  if (!body.fontFamily.includes("IBM Plex Sans")) probs.push("body font not Plex: " + body.fontFamily);
  if (body.backgroundColor !== "rgb(246, 249, 250)") probs.push("bg not tile: " + body.backgroundColor);
  const nav = document.querySelector("nav .tab");
  if (!nav) probs.push("tab bar missing/unstyled");
  const h1 = document.querySelector("h1");
  if (h1 && !getComputedStyle(h1).fontFamily.includes("Barlow")) probs.push("h1 not display font");
  return probs;
}
"""

failures = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(f"{BASE}/login")
    page.fill("input[name=username]", "owner")
    page.fill("input[name=password]", PASSWORD)
    page.click("button")
    page.wait_for_load_state("networkidle")
    for path in SCREENS:
        page.goto(f"{BASE}{path}")
        page.wait_for_timeout(600)
        problems = page.evaluate(CHECKS)
        if problems:
            failures.append((path, problems))
        if SHOTS:
            name = path.strip("/").replace("/", "_") or "today"
            page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)

    # Tech pass
    page.goto(f"{BASE}/users")
    if "visual-tech" not in page.content():
        page.goto(f"{BASE}/users/new")
        page.fill("input[name=name]", "Visual Tech")
        page.fill("input[name=username]", "visual-tech")
        page.fill("input[name=password]", os.environ.get("VC_TECH_PASSWORD", "visual-pass-1"))
        page.click("button.btn-primary")
    tctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    tpage = tctx.new_page()
    tpage.goto(f"{BASE}/login")
    tpage.fill("input[name=username]", "visual-tech")
    tpage.fill("input[name=password]", os.environ.get("VC_TECH_PASSWORD", "visual-pass-1"))
    tpage.click("button")
    tpage.wait_for_load_state("networkidle")
    tpage.goto(f"{BASE}/")
    content = tpage.content()
    for forbidden in ['href="/money"', 'href="/settings"']:
        if forbidden in content:
            failures.append(("/ (tech)", [f"tech nav leaks {forbidden}"]))
    probs = tpage.evaluate(CHECKS)
    if probs:
        failures.append(("/ (tech)", probs))
    tctx.close()

    # Desktop pass: sidebar visible, tab bar hidden at lg: viewport.
    dctx = browser.new_context(viewport={"width": 1280, "height": 900})
    dpage = dctx.new_page()
    dpage.goto(f"{BASE}/login")
    dpage.fill("input[name=username]", "owner")
    dpage.fill("input[name=password]", PASSWORD)
    dpage.click("button")
    dpage.wait_for_load_state("networkidle")
    dpage.goto(f"{BASE}/")
    dpage.wait_for_timeout(600)
    desktop_probs = dpage.evaluate("""
() => {
  const probs = [];
  const sidebar = document.querySelector("#sidebar");
  const tabbar = document.querySelector("#tabbar");
  if (!sidebar || getComputedStyle(sidebar).display === "none") probs.push("sidebar hidden at desktop viewport");
  if (!tabbar || getComputedStyle(tabbar).display !== "none") probs.push("tabbar visible at desktop viewport");
  return probs;
}
""")
    if desktop_probs:
        failures.append(("/ (desktop)", desktop_probs))
    dctx.close()

    browser.close()

if failures:
    for path, problems in failures:
        print(f"FAIL {path}: {'; '.join(problems)}")
    sys.exit(1)
print(f"OK — {len(SCREENS)} screens styled correctly")

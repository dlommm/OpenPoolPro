"""Visual smoke check: proves the design system actually renders on every screen.

Catches the failure class where markup ships but the stylesheet is stale or
broken (naked-HTML pages). Run against a live instance:

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py [shots_dir]

Requires: pip install playwright && playwright install chromium
Exit code 0 = all screens styled; non-zero = a screen failed a check.
"""
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("APP_URL", "http://localhost:8000")
PASSWORD = os.environ.get("APP_PASSWORD", "coastline-demo")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None

SCREENS = ["/", "/week", "/customers", "/customers/1", "/jobs", "/money", "/settings",
           "/account", "/audit", "/inventory", "/routes/balance"]

# Per-theme expected computed styles. THEME selects which theme is active
# (set it via the Settings control or `set_setting` before running); the map
# defaults to warm-coastal, whose values are the base defaults in
# css/base.css. Check a non-default theme only once its css/themes/<theme>.css
# is filled in. fresh-field values are its light-mode tokens (Playwright's
# default color scheme is light).
THEME = os.environ.get("THEME", "warm-coastal")
EXPECTED = {
    "warm-coastal": {"body_font": "system-ui", "display_font": "system-ui",
                     "body_bg": "rgb(243, 237, 227)"},   # tile #F3EDE3
    # Light-mode tokens, for the same reason fresh-field uses them: with no
    # `mode` in localStorage no data-mode attribute is set, so the theme
    # follows prefers-color-scheme, and Playwright's default is light. The
    # dark tile (#0E141B) is what a device in dark mode gets.
    "control-room": {"body_font": "Helvetica Neue", "display_font": "Helvetica Neue",
                     "body_bg": "rgb(234, 239, 244)"},    # tile #EAEFF4 (light)
    "fresh-field": {"body_font": "system-ui", "display_font": "system-ui",
                    "body_bg": "rgb(247, 250, 252)"},     # tile #F7FAFC (light)
}
EXPECT = EXPECTED.get(THEME, EXPECTED["warm-coastal"])

# Computed-style assertions: proves app.css APPLIED, not merely returned 200.
# Expected values are passed in per theme (see EXPECT above).
CHECKS = """
(exp) => {
  const body = getComputedStyle(document.body);
  const probs = [];
  if (!body.fontFamily.includes(exp.body_font)) probs.push("body font not " + exp.body_font + ": " + body.fontFamily);
  if (body.backgroundColor !== exp.body_bg)
    probs.push("bg not tile (want " + exp.body_bg + "): " + body.backgroundColor);
  const topbar = document.querySelector("#topbar");
  if (!topbar) probs.push("topbar missing");
  else if (getComputedStyle(topbar).position !== "sticky") probs.push("topbar not sticky/unstyled");
  const h1 = document.querySelector("h1");
  if (h1 && !getComputedStyle(h1).fontFamily.includes(exp.display_font))
    probs.push("h1 not display font (want " + exp.display_font + ")");

  // Control geometry. Both of these shipped once and neither is visible in a
  // 200 or in a font/colour assertion: a button with no horizontal padding
  // clipped its own label ("Generate for 2026-09"), and a button class that
  // resolves to `flex` rather than `inline-flex` turns every <a> wearing it
  // into a full-width bar across the page.
  const label = (el) => (el.textContent || "").trim().slice(0, 30);
  for (const el of document.querySelectorAll(".btn-primary, .btn-secondary, .btn-danger")) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (parseFloat(cs.paddingLeft) < 8 || parseFloat(cs.paddingRight) < 8)
      probs.push("button has no horizontal padding: " + label(el));
    if (el.scrollWidth > el.clientWidth + 1)
      probs.push("button label is clipped: " + label(el));
    if (el.matches("a") && r.width > 0.9 * document.documentElement.clientWidth
        && cs.display === "flex")
      probs.push("link-as-button stretches full width (use inline-flex): " + label(el));
  }
  // Nothing should push the page itself sideways; wide tables scroll in their
  // own overflow-x container instead.
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1)
    probs.push("page scrolls horizontally by " + (de.scrollWidth - de.clientWidth) + "px");
  return probs;
}
"""

# A bare YYYY-MM-DD in body text is a date that was never formatted for a
# person. /audit is exempt: it is a technical log where a precise, sortable
# timestamp is the point.
ISO_EXEMPT = {"/audit"}
_ISO = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")


def _raw_iso_dates(page, path):
    if path in ISO_EXEMPT:
        return []
    found = sorted(set(_ISO.findall(page.inner_text("body"))))
    if not found:
        return []
    return [f"raw ISO date shown to the user: {', '.join(found[:3])}"]


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
        problems = page.evaluate(CHECKS, EXPECT)
        problems += _raw_iso_dates(page, path)
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
    probs = tpage.evaluate(CHECKS, EXPECT)
    if probs:
        failures.append(("/ (tech)", probs))
    tctx.close()

    # Tablet-portrait pass (iPad): topbar still in charge, md: width tier applied.
    tabctx = browser.new_context(viewport={"width": 834, "height": 1194}, device_scale_factor=2)
    tabpage = tabctx.new_page()
    tabpage.goto(f"{BASE}/login")
    tabpage.fill("input[name=username]", "owner")
    tabpage.fill("input[name=password]", PASSWORD)
    tabpage.click("button")
    tabpage.wait_for_load_state("networkidle")
    for path in ["/", "/week", "/dashboard"]:
        tabpage.goto(f"{BASE}{path}")
        tabpage.wait_for_timeout(600)
        tablet_probs = tabpage.evaluate("""
() => {
  const probs = [];
  const topbar = document.querySelector("#topbar");
  const sidebar = document.querySelector("#sidebar");
  if (!topbar || getComputedStyle(topbar).display === "none") probs.push("topbar hidden at tablet viewport");
  if (sidebar && getComputedStyle(sidebar).display !== "none") probs.push("sidebar visible at tablet viewport");
  const main = document.querySelector("main");
  if (main && main.offsetWidth < 600) probs.push("main not using md width tier: " + main.offsetWidth + "px");
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth)
    probs.push("horizontal overflow at tablet viewport");
  return probs;
}
""")
        if tablet_probs:
            failures.append((f"{path} (tablet)", tablet_probs))
        if SHOTS:
            name = (path.strip("/").replace("/", "_") or "today") + "_tablet"
            tabpage.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
    tabctx.close()

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
  const topbar = document.querySelector("#topbar");
  if (!sidebar || getComputedStyle(sidebar).display === "none") probs.push("sidebar hidden at desktop viewport");
  if (!topbar || getComputedStyle(topbar).display !== "none") probs.push("topbar visible at desktop viewport");
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
print(f"OK — {len(SCREENS)} screens styled correctly for theme '{THEME}'")

"""Visual smoke check: proves the design system actually renders on every screen.

Catches the failure class where markup ships but the stylesheet is stale or
broken (naked-HTML pages). Run against a live instance:

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py [shots_dir]

Requires: pip install playwright && playwright install chromium
Exit code 0 = all screens styled; non-zero = a screen failed a check.

Point this at a disposable instance (CI, a dev box) -- not production. To
reach the screens only a real session can reach, it writes: it creates a
"Visual Tech" staff member, and it issues then revokes a portal link for
customer 1, restoring the Public base URL it borrowed to do so.
"""
import os
import re
import sys

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE = os.environ.get("APP_URL", "http://localhost:8000")
PASSWORD = os.environ.get("APP_PASSWORD", "coastline-demo")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None

# Every page a staff user can reach. The list was previously 11 screens, and
# /analytics -- which renders a bare ISO week-start date -- sat in the gap.
# A check is only worth what it covers.
SCREENS = ["/", "/dashboard", "/week", "/customers", "/customers/1", "/jobs", "/quotes",
           "/contracts", "/money", "/invoices", "/ar", "/analytics", "/inventory", "/settings",
           "/billing", "/audit", "/account", "/routes/balance", "/routes/plan", "/operations",
           "/users", "/followups", "/water-alerts", "/search", "/today/print",
           "/customers/1/portal",
           # Form pages. They are screens like any other -- a form that renders
           # its own page can clip a label or stretch a button just as a list can.
           "/customers/new", "/customers/import", "/jobs/new", "/quotes/new",
           "/contracts/new", "/contracts/renew", "/users/new"]

# Per-theme expected computed styles. THEME selects which theme is active
# (set it via the Settings control or `set_setting` before running); the map
# defaults to warm-coastal, whose values are the base defaults in
# css/base.css. Check a non-default theme only once its css/themes/<theme>.css
# is filled in. fresh-field values are its light-mode tokens (Playwright's
# default color scheme is light).
THEME = os.environ.get("THEME", "warm-coastal")
# body_bg is the light tile, dark_bg the dark one: with no `mode` in
# localStorage no data-mode attribute is set, so the theme follows
# prefers-color-scheme, and the dark pass below drives that directly.
EXPECTED = {
    "warm-coastal": {"body_font": "system-ui", "display_font": "system-ui",
                     "body_bg": "rgb(243, 237, 227)",     # tile #F3EDE3
                     "dark_bg": "rgb(18, 32, 31)"},       # tile #12201F
    "control-room": {"body_font": "Helvetica Neue", "display_font": "Helvetica Neue",
                     "body_bg": "rgb(234, 239, 244)",     # tile #EAEFF4
                     "dark_bg": "rgb(14, 20, 27)"},       # tile #0E141B
    "fresh-field": {"body_font": "system-ui", "display_font": "system-ui",
                    "body_bg": "rgb(247, 250, 252)",      # tile #F7FAFC
                    "dark_bg": "rgb(11, 18, 32)"},        # tile #0B1220
}
EXPECT = EXPECTED.get(THEME, EXPECTED["warm-coastal"])
STAFF = {**EXPECT, "chrome": "staff"}
CUSTOMER = {**EXPECT, "chrome": "none"}
DARK = {**STAFF, "body_bg": EXPECT["dark_bg"]}
DARK_CUSTOMER = {**CUSTOMER, "body_bg": EXPECT["dark_bg"]}

# /settings is seven panels behind CSS radio tabs, and only the first one is
# rendered by a plain visit -- six screens' worth of controls sat outside this
# check while looking covered by the "/settings" entry above.
SETTINGS_TABS = ["business", "pricing", "chemistry", "service", "crew", "system", "contract"]

# Computed-style assertions: proves app.css APPLIED, not merely returned 200.
# Expected values are passed in per theme (see EXPECT above).
CHECKS = r"""
(exp) => {
  const body = getComputedStyle(document.body);
  const probs = [];
  if (!body.fontFamily.includes(exp.body_font)) probs.push("body font not " + exp.body_font + ": " + body.fontFamily);
  if (body.backgroundColor !== exp.body_bg)
    probs.push("bg not tile (want " + exp.body_bg + "): " + body.backgroundColor);
  // exp.chrome says which shell this page is supposed to wear. Staff pages
  // ride the sticky topbar; the customer portal has no account behind it and
  // must never render staff navigation -- a link into /money it cannot open
  // would be the giveaway that the wrong base template got picked up.
  const topbar = document.querySelector("#topbar");
  if (exp.chrome === "staff") {
    if (!topbar) probs.push("topbar missing");
    else if (getComputedStyle(topbar).position !== "sticky") probs.push("topbar not sticky/unstyled");
  } else if (topbar || document.querySelector("#sidebar")) {
    probs.push("customer page renders staff navigation");
  }
  const h1 = document.querySelector("h1");
  if (!h1 && exp.chrome === "none") probs.push("customer page has no heading");
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

  // Readability, not taste. A palette that was only half filled in for a mode
  // renders as pale-grey-on-white, which passes every check above.
  const chans = (s) => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => {
    const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const l1 = lum(chans(body.color)), l2 = lum(chans(body.backgroundColor));
  const contrast = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  // Every theme currently lands at 11.9:1 light and 14.7:1 dark, so the bar is
  // WCAG AAA (7:1) rather than the AA minimum: it still leaves real headroom,
  // and a palette that drops below it has regressed, not merely been restyled.
  if (contrast < 7)
    probs.push("body text contrast is " + contrast.toFixed(1) + ":1 (want 7:1)");
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


def visit(page, url):
    """goto with one retry. Chromium occasionally aborts a navigation while the
    service worker is installing (net::ERR_ABORTED) -- a flake, not a defect,
    and a check that cries wolf stops being read."""
    try:
        return page.goto(url)
    except PlaywrightError:
        page.wait_for_timeout(500)
        return page.goto(url)


failures = []
checked = 0
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = ctx.new_page()
    visit(page, f"{BASE}/login")
    page.fill("input[name=username]", "owner")
    page.fill("input[name=password]", PASSWORD)
    page.click("button")
    page.wait_for_load_state("networkidle")
    for path in SCREENS:
        visit(page, f"{BASE}{path}")
        page.wait_for_timeout(600)
        problems = page.evaluate(CHECKS, STAFF)
        problems += _raw_iso_dates(page, path)
        if problems:
            failures.append((path, problems))
        if SHOTS:
            name = path.strip("/").replace("/", "_") or "today"
            page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
        checked += 1

    # Tech pass
    visit(page, f"{BASE}/users")
    if "visual-tech" not in page.content():
        visit(page, f"{BASE}/users/new")
        page.fill("input[name=name]", "Visual Tech")
        page.fill("input[name=username]", "visual-tech")
        page.fill("input[name=password]", os.environ.get("VC_TECH_PASSWORD", "visual-pass-1"))
        page.click("button.btn-primary")
    tctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    tpage = tctx.new_page()
    visit(tpage, f"{BASE}/login")
    tpage.fill("input[name=username]", "visual-tech")
    tpage.fill("input[name=password]", os.environ.get("VC_TECH_PASSWORD", "visual-pass-1"))
    tpage.click("button")
    tpage.wait_for_load_state("networkidle")
    visit(tpage, f"{BASE}/")
    content = tpage.content()
    for forbidden in ['href="/money"', 'href="/settings"']:
        if forbidden in content:
            failures.append(("/ (tech)", [f"tech nav leaks {forbidden}"]))
    probs = tpage.evaluate(CHECKS, STAFF)
    if probs:
        failures.append(("/ (tech)", probs))
    checked += 1

    # The permission wall is a screen too: a tech who taps through to /money
    # should land on the styled 403, not a raw JSON blob.
    resp = visit(tpage, f"{BASE}/money")
    if resp.status != 403:
        failures.append(("/money (tech)", [f"admin-only page answered {resp.status}, not 403"]))
    probs = tpage.evaluate(CHECKS, STAFF)
    if probs:
        failures.append(("/money (tech 403)", probs))
    if SHOTS:
        tpage.screenshot(path=f"{SHOTS}/403.png", full_page=True)
    checked += 1
    tctx.close()

    # Every settings panel, not just the one the radio tabs open on.
    visit(page, f"{BASE}/settings")
    for tab in SETTINGS_TABS:
        page.click(f"label.tab-label[for=settab-{tab}]")
        page.wait_for_timeout(250)
        problems = page.evaluate(CHECKS, STAFF)
        problems += _raw_iso_dates(page, f"/settings#{tab}")
        if problems:
            failures.append((f"/settings#{tab}", problems))
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/settings_{tab}.png", full_page=True)
        checked += 1

    # The staff error page. Until v1.24.5 these routes answered a browser with
    # raw JSON filling the window, and nothing here would have noticed.
    resp = visit(page, f"{BASE}/customers/99999999")
    if resp.status != 404:
        failures.append(("/customers/<missing>", [f"answered {resp.status}, not 404"]))
    problems = page.evaluate(CHECKS, STAFF)
    if not page.query_selector(".page-eyebrow"):
        problems.append("404 rendered without the error page shell (raw JSON?)")
    if problems:
        failures.append(("/customers/<missing> (404)", problems))
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/404.png", full_page=True)
    checked += 1

    # --- Customer portal --------------------------------------------------
    # The one surface a customer ever sees, and nothing checked it: this
    # script signs in as staff, and the portal opens on a customer token.
    # Issue one through the staff UI, then open it in a context holding no
    # session at all -- which is how a customer arrives.
    visit(page, f"{BASE}/settings")
    page.click("label.tab-label[for=settab-system]")
    saved_public_base = page.input_value("input[name=public_base_url]")
    # Issuing refuses a base URL that isn't HTTPS. The value only shapes the
    # link text; the token is the part that matters, and it is opened on BASE.
    page.fill("input[name=public_base_url]", "https://visual-check.example")
    page.click("form[action='/settings/public-url'] button")
    page.wait_for_load_state("networkidle")
    visit(page, f"{BASE}/customers/1/portal")
    page.click("form[action$='/portal/issue'] button")
    page.wait_for_load_state("networkidle")
    token = page.input_value("#portal-link").rsplit("/", 1)[-1]

    cctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    cpage = cctx.new_page()
    # A live link, then a dead one: the customer holding an expired or mistyped
    # link gets a finished page too, and it is the page nobody ever looks at.
    for name, path in [("portal", f"/portal/{token}"),
                       ("portal_invalid", "/portal/" + "z" * 48)]:
        resp = visit(cpage, f"{BASE}{path}")
        cpage.wait_for_timeout(600)
        probs = cpage.evaluate(CHECKS, CUSTOMER)
        probs += _raw_iso_dates(cpage, path)
        if name == "portal_invalid" and resp.status != 404:
            probs.append(f"dead portal link answered {resp.status}, not 404")
        if probs:
            failures.append((f"/{name}", probs))
        if SHOTS:
            cpage.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
        checked += 1
    cctx.close()

    # The same portal at night, on the customer's own phone. This has to run
    # while the link is still live, hence here rather than with the dark staff
    # pass below.
    dkcctx = browser.new_context(viewport={"width": 390, "height": 844},
                                 device_scale_factor=2, color_scheme="dark")
    dkcpage = dkcctx.new_page()
    visit(dkcpage, f"{BASE}/portal/{token}")
    dkcpage.wait_for_timeout(400)
    problems = dkcpage.evaluate(CHECKS, DARK_CUSTOMER)
    if problems:
        failures.append(("/portal (dark)", problems))
    if SHOTS:
        dkcpage.screenshot(path=f"{SHOTS}/portal_dark.png", full_page=True)
    checked += 1
    dkcctx.close()

    # Put the instance back the way it was found.
    visit(page, f"{BASE}/customers/1/portal")
    page.click("form[action$='/portal/revoke'] button")
    page.wait_for_load_state("networkidle")
    visit(page, f"{BASE}/settings")
    page.click("label.tab-label[for=settab-system]")
    page.fill("input[name=public_base_url]", saved_public_base)
    page.click("form[action='/settings/public-url'] button")
    page.wait_for_load_state("networkidle")

    # Dark mode. Every theme ships a dark palette and nothing had ever
    # rendered one -- Playwright reports a light device, so a dark tile that
    # was never filled in looked exactly like a pass. This drives the device
    # preference directly, which is what a customer's phone does at night.
    dkctx = browser.new_context(viewport={"width": 390, "height": 844},
                                device_scale_factor=2, color_scheme="dark")
    dkpage = dkctx.new_page()
    visit(dkpage, f"{BASE}/login")
    dkpage.fill("input[name=username]", "owner")
    dkpage.fill("input[name=password]", PASSWORD)
    dkpage.click("button")
    dkpage.wait_for_load_state("networkidle")
    for path in SCREENS:
        visit(dkpage, f"{BASE}{path}")
        dkpage.wait_for_timeout(400)
        problems = dkpage.evaluate(CHECKS, DARK)
        if problems:
            failures.append((f"{path} (dark)", problems))
        if SHOTS:
            name = (path.strip("/").replace("/", "_") or "today") + "_dark"
            dkpage.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
        checked += 1
    dkctx.close()

    # Tablet-portrait pass (iPad): topbar still in charge, md: width tier applied.
    tabctx = browser.new_context(viewport={"width": 834, "height": 1194}, device_scale_factor=2)
    tabpage = tabctx.new_page()
    visit(tabpage, f"{BASE}/login")
    tabpage.fill("input[name=username]", "owner")
    tabpage.fill("input[name=password]", PASSWORD)
    tabpage.click("button")
    tabpage.wait_for_load_state("networkidle")
    for path in ["/", "/week", "/dashboard"]:
        visit(tabpage, f"{BASE}{path}")
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
        checked += 1
    tabctx.close()

    # Desktop pass: sidebar visible, tab bar hidden at lg: viewport.
    dctx = browser.new_context(viewport={"width": 1280, "height": 900})
    dpage = dctx.new_page()
    visit(dpage, f"{BASE}/login")
    dpage.fill("input[name=username]", "owner")
    dpage.fill("input[name=password]", PASSWORD)
    dpage.click("button")
    dpage.wait_for_load_state("networkidle")
    visit(dpage, f"{BASE}/")
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
    checked += 1
    dctx.close()

    browser.close()

if failures:
    for path, problems in failures:
        print(f"FAIL {path}: {'; '.join(problems)}")
    sys.exit(1)
print(f"OK — {checked} renders styled correctly for theme '{THEME}': staff screens "
      f"light and dark, settings panels, error pages, the customer portal, "
      f"and the tablet and desktop layouts")

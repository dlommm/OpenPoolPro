"""CSP nonce discipline, enforced at the source level.

The CSP carries no 'unsafe-inline' for scripts: an inline <script> without
nonce="{{ csp_nonce() }}" silently does nothing, and an inline on*= handler
attribute is dead regardless of any nonce. Neither failure raises anything —
the page just quietly loses behavior — so this suite makes both impossible
to commit rather than trying to notice them at runtime.
"""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).parent.parent / "app" / "templates"

# Attribute-position inline handlers: whitespace, then on<event>=.
# (Also matches the same pattern inside JS-built HTML strings, on purpose —
# CSP kills those handlers too, no matter how the markup reaches the DOM.)
_HANDLER_RE = re.compile(r"""[\s"']on[a-z]+\s*=""")
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_NONCE_MARK = 'nonce="{{ csp_nonce() }}"'


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_inline_event_handlers_anywhere():
    offenders = []
    for path in _templates():
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if _HANDLER_RE.search(line):
                offenders.append(f"{path.relative_to(TEMPLATES)}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "inline on*= handlers are dead under the nonce CSP — convert to "
        "addEventListener/delegation (see _behaviors.html):\n" + "\n".join(offenders))


def test_every_inline_script_carries_the_nonce():
    offenders = []
    for path in _templates():
        for tag in _SCRIPT_TAG_RE.findall(path.read_text()):
            if "src=" in tag:
                continue  # same-origin file scripts are allowed by 'self'
            if 'type="application/json"' in tag:
                continue  # data block, not executable — CSP doesn't apply
            if _NONCE_MARK not in tag:
                offenders.append(f"{path.relative_to(TEMPLATES)}: {tag[:100]}")
    assert not offenders, (
        f"inline <script> blocks must carry {_NONCE_MARK} or the browser "
        "silently drops them:\n" + "\n".join(offenders))


def test_csp_header_uses_nonce_and_page_scripts_match(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp.split("style-src")[0], \
        "script-src must not allow unsafe-inline"
    m = re.search(r"script-src [^;]*'nonce-([^']+)'", csp)
    assert m, csp
    nonce = m.group(1)
    inline_tags = [t for t in _SCRIPT_TAG_RE.findall(r.text) if "src=" not in t]
    assert inline_tags, "login page should have inline scripts"
    for tag in inline_tags:
        assert f'nonce="{nonce}"' in tag, tag

    # Nonces must differ per request, or they aren't nonces.
    r2 = client.get("/login")
    m2 = re.search(r"'nonce-([^']+)'", r2.headers["content-security-policy"])
    assert m2.group(1) != nonce


def test_authed_page_and_error_page_scripts_match_header(client):
    for url in ("/settings", "/contracts"):
        r = client.get(url)
        assert r.status_code == 200
        nonce = re.search(r"'nonce-([^']+)'",
                          r.headers["content-security-policy"]).group(1)
        for tag in _SCRIPT_TAG_RE.findall(r.text):
            if "src=" not in tag:
                assert f'nonce="{nonce}"' in tag, (url, tag)

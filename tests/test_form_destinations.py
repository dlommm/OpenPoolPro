"""Submitting a form keeps you where you were.

Landing on a different page after a save loses the operator's place and their
sense of what just happened. A form returns to the page that hosted it unless
the destination IS the outcome -- a created job, a completed login.

This walks every template's forms, resolves each action to its handler, and
compares where that handler sends you against the page the form lives on. New
cross-page redirects fail here until someone adds them to DELIBERATE below with
a reason, so the rule cannot erode by accident.

Scope, so nobody reads more into a green run than it earns: only redirects
written directly in the handler are seen. One returned by a shared helper --
_qbo_fail sending an unconnected admin to the connection settings, say -- is
invisible here, because following calls would flag those legitimate cases as
often as real ones. Helper redirects still need a human to think about where
they land.
"""
import ast
import pathlib
import re

import pytest

ROUTERS = pathlib.Path("app/routers")
TEMPLATES = pathlib.Path("app/templates")

# (form action, redirect target prefix) -> why leaving the page is correct.
DELIBERATE = {
    ("/login", "/"): "signed in; go to the app",
    ("/login", "/login/2fa"): "password accepted; collect the second factor",
    ("/logout", "/login"): "signed out; nowhere else to be",
    ("/water-alerts/inspect", "/jobs/"): "the job it just created is the outcome",
    ("/contracts/renew", "/contracts"): "the renewal wizard is finished",
}


def _page_of(target: str) -> str:
    return target.split("?")[0].split("#")[0].rstrip("/") or "/"


def _module_constants(tree) -> dict[str, str]:
    """Module-level string constants, so a redirect built from one resolves."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _literal(node, constants: dict[str, str]) -> str | None:
    """Best-effort static value of a redirect target.

    Handles a plain string, a name bound to one, and an f-string -- whose
    interpolations are irrelevant here because only the path before '?' matters.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                resolved = _literal(part.value, constants)
                # An unresolved interpolation can only appear mid-path once the
                # literal prefix has been captured; a placeholder keeps the
                # prefix comparable without inventing a path.
                out.append(resolved if resolved is not None else "\x00")
        return "".join(out)
    return None


def _route_decorators(node):
    for dec in getattr(node, "decorator_list", []):
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Attribute) and func.attr in ("get", "post") \
                and isinstance(func.value, ast.Name) and func.value.id == "router":
            args = dec.args if isinstance(dec, ast.Call) else []
            if args and isinstance(args[0], ast.Constant):
                yield func.attr, args[0].value


def _handler_redirects() -> dict[str, list[str]]:
    """Route path -> redirect targets found INSIDE that handler only.

    Parsed rather than pattern-matched: helpers defined between two route
    decorators would otherwise have their redirects credited to whichever
    route happened to precede them.
    """
    routes: dict[str, list[str]] = {}
    for path_file in ROUTERS.glob("*.py"):
        tree = ast.parse(path_file.read_text())
        constants = _module_constants(tree)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            paths = [path for _, path in _route_decorators(node)]
            if not paths:
                continue
            targets = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                        and inner.func.id == "RedirectResponse" and inner.args:
                    value = _literal(inner.args[0], constants)
                    if value is not None:
                        targets.append(value)
            for path in paths:
                routes.setdefault(path, []).extend(targets)
    return routes


def _template_pages() -> dict[str, str]:
    """Template name -> the GET route that renders it."""
    pages: dict[str, str] = {}
    for path_file in ROUTERS.glob("*.py"):
        tree = ast.parse(path_file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            gets = [p for method, p in _route_decorators(node) if method == "get"]
            if not gets:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr == "TemplateResponse" and len(inner.args) >= 2 \
                        and isinstance(inner.args[1], ast.Constant):
                    pages.setdefault(inner.args[1].value, gets[0])
    return pages


def _crossings():
    routes, pages = _handler_redirects(), _template_pages()
    out = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        name = str(template.relative_to(TEMPLATES))
        if template.name.startswith("_"):
            # A partial is re-rendered into a page it does not own (the O365
            # poll fragment, the invoice list). Its forms belong to the host
            # page, so the host page's own entry is what governs them.
            continue
        host = pages.get(name)
        if not host or "{" in _page_of(host):
            continue  # not a page of its own, or a parameterised path
        for action in sorted(set(re.findall(r'<form[^>]*action="([^"]+)"', template.read_text()))):
            for target in routes.get(action, []):
                if _page_of(target) != _page_of(host):
                    out.append((name, action, target, _page_of(host)))
    return out


def test_forms_return_to_the_page_they_were_submitted_from():
    unexplained = []
    for name, action, target, host in _crossings():
        if any(action == a and target.startswith(prefix) for a, prefix in DELIBERATE):
            continue
        unexplained.append(
            f"{name}: form -> {action} lands on {target!r}, but the page is {host}")
    assert not unexplained, (
        "These forms move the operator to a different page on submit.\n"
        "Redirect back to the hosting page, or add an entry to DELIBERATE "
        "explaining why leaving is the right outcome:\n  " + "\n  ".join(unexplained))


def test_the_deliberate_list_has_no_stale_entries():
    """A allowlist nobody prunes stops describing the code."""
    live = {(action, target) for _, action, target, _ in _crossings()}
    stale = [entry for entry in DELIBERATE
             if not any(a == entry[0] and t.startswith(entry[1]) for a, t in live)]
    assert not stale, f"DELIBERATE entries no longer match any form: {stale}"


@pytest.mark.parametrize("route", ["/settings/qbo/customers/push", "/settings/qbo/customers/import"])
def test_customer_sync_returns_to_the_review_page(route):
    """Regression: both landed on /settings, dropping the operator out of the
    review they were working through."""
    targets = _handler_redirects()[route]
    assert targets, f"{route} has no redirect"
    for target in targets:
        assert _page_of(target) == "/settings/qbo/customers", target

"""Foundation tests for the runtime-swappable multi-theme system.

Covers the two load-bearing pieces Foundation owns:
  1. ThemeAwareLoader resolution order (unit, driven by the ContextVar).
  2. The active_theme Setting wiring through the middleware to base.html's
     data-theme attribute (integration, via the shared `client` fixture).
"""
import jinja2

from app.main import ThemeAwareLoader, active_theme_var


def test_loader_prefers_theme_then_falls_back(tmp_path):
    """themes/<theme>/<name> wins when present; missing override falls back to
    the shared template; and a name already under themes/ is looked up verbatim."""
    root = tmp_path / "templates"
    (root / "themes" / "control-room").mkdir(parents=True)
    (root / "page.html").write_text("SHARED-PAGE")
    (root / "themes" / "control-room" / "page.html").write_text("THEMED-PAGE")
    (root / "only-shared.html").write_text("ONLY-SHARED")

    env = jinja2.Environment(loader=ThemeAwareLoader(str(root)))

    # Theme override present -> theme file wins.
    token = active_theme_var.set("control-room")
    try:
        assert env.get_template("page.html").render() == "THEMED-PAGE"
        # No override for this name -> shared template.
        assert env.get_template("only-shared.html").render() == "ONLY-SHARED"
        # An explicit themes/... name is resolved verbatim (no re-prefixing).
        assert env.get_template("themes/control-room/page.html").render() == "THEMED-PAGE"
    finally:
        active_theme_var.reset(token)

    # A theme with no override directory -> shared template.
    token = active_theme_var.set("warm-coastal")
    try:
        assert env.get_template("page.html").render() == "SHARED-PAGE"
    finally:
        active_theme_var.reset(token)


def test_missing_template_raises(tmp_path):
    """A name that exists in neither the theme nor the shared root still raises
    TemplateNotFound (loader does not swallow genuine misses)."""
    root = tmp_path / "templates"
    root.mkdir()
    env = jinja2.Environment(loader=ThemeAwareLoader(str(root)))
    token = active_theme_var.set("control-room")
    try:
        try:
            env.get_template("nope.html")
            assert False, "expected TemplateNotFound"
        except jinja2.TemplateNotFound:
            pass
    finally:
        active_theme_var.reset(token)


def test_active_theme_setting_drives_data_theme(client, engine):
    """Setting active_theme to control-room makes the middleware set
    request.state.active_theme, which base.html renders as data-theme."""
    from sqlalchemy.orm import Session

    from app import models

    # Default (no setting yet) is warm-coastal.
    assert 'data-theme="warm-coastal"' in client.get("/").text

    with Session(engine) as s:
        models.set_setting(s, "active_theme", "control-room")
        s.commit()

    html = client.get("/").text
    assert 'data-theme="control-room"' in html
    # theme-color meta swaps with the theme too.
    assert 'content="#0E141B"' in html


def test_invalid_stored_theme_falls_back(client, engine):
    """A tampered/unknown stored theme is coerced back to the default so the
    page never renders an out-of-set data-theme."""
    from sqlalchemy.orm import Session

    from app import models

    with Session(engine) as s:
        models.set_setting(s, "active_theme", "bogus-theme")
        s.commit()

    assert 'data-theme="warm-coastal"' in client.get("/").text

import pytest
from sqlalchemy.orm import Session

from app import models, palettes


def test_clean_hex():
    assert palettes.clean_hex("1b6c80") == "#1B6C80"
    assert palettes.clean_hex(" #ABCDEF ") == "#ABCDEF"
    for bad in ("", None, "#12345", "red", "#1B6C80;}body{display:none", "url(x)"):
        assert palettes.clean_hex(bad) is None


@pytest.mark.parametrize("base", ["#FFE14D", "#FFFFFF", "#000000", "#00FF00", "#2563B8",
                                  "#D0603A", "#6A55C9", "#808080", "#FF00FF"])
def test_any_base_stays_readable(base):
    light, dark = palettes.derive(base, "light"), palettes.derive(base, "dark")
    assert palettes.ratio(light["--a"], "#FFFFFF") >= 4.5
    assert palettes.ratio(light["--ai"], light["--as"]) >= 7
    assert palettes.ratio(light["--t"], "#FFFFFF") >= 7
    for k in ("--mast-a", "--mast-b", "--mast-c", "--hero-a", "--hero-b", "--hero-c"):
        assert palettes.ratio("#FFFFFF", light[k]) >= 6, k
        assert palettes.ratio("#FFFFFF", dark[k]) >= 6, k
    assert palettes.ratio(dark["--a"], dark["--sheet"]) >= 4.5
    assert palettes.ratio(dark["--ai"], dark["--as"]) >= 7


def test_lagoon_emits_nothing_and_others_scope_by_mode():
    assert palettes.css("lagoon") == ""
    assert palettes.css("nonsense") == ""
    assert palettes.css("custom", "not a colour") == ""
    out = palettes.css("coral")
    assert out.startswith(":root:not(.dark){") and ":root.dark{" in out


def test_save_and_apply_palette(client, engine):
    client.post("/settings/appearance", data={"palette": "custom", "palette_custom": "d0603a"})
    with Session(engine) as s:
        assert models.get_setting(s, "palette", "") == "custom"
        assert models.get_setting(s, "palette_custom", "") == "#D0603A"
    page = client.get("/settings").text
    assert "<style>:root:not(.dark){" in page
    # Garbage keeps what was stored and never reaches the page.
    client.post("/settings/appearance", data={"palette": "</style>", "palette_custom": "#12;}"})
    with Session(engine) as s:
        assert models.get_setting(s, "palette", "") == "custom"
        assert models.get_setting(s, "palette_custom", "") == "#D0603A"
    client.post("/settings/appearance", data={"palette": "lagoon"})
    assert "<style>:root" not in client.get("/settings").text


def test_stored_garbage_falls_back(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "palette", "custom")
        models.set_setting(s, "palette_custom", "#12;}body{x:y")
        s.commit()
    page = client.get("/settings").text
    assert "body{x:y" not in page

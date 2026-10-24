"""Company colour palettes: light and dark tokens derived from one base colour.

Every derived colour is solved for contrast -- accent text 4.6:1 on its
surface, accent on its tint 7:1, white on the masthead and hero 6:1 -- so any
base, a custom pick included, stays WCAG AA. Lagoon keeps the hand-tuned Aqua
Atelier tokens in css/v2.css and emits nothing. Chart colours never change,
so they stay safe for colour-blind readers.

The page receives the result as a <style> block of CSS custom properties;
the only user input that reaches it is a hex colour that passed `clean_hex`.

Pure: no FastAPI, no database.
"""
import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Palette:
    key: str
    name: str
    desc: str
    base: str


PALETTES = (
    Palette("lagoon", "Lagoon", "Teal and marine. The Aqua Atelier default.", "#1B6C80"),
    Palette("reef", "Deep Reef", "Ocean blue with navy ink. Crisp and corporate.", "#2563B8"),
    Palette("seaglass", "Sea Glass", "Weathered green. Calm, outdoors, eco-minded.", "#2F8A63"),
    Palette("coral", "Coral Coast", "Warm terracotta. Friendly and energetic.", "#D0603A"),
    Palette("twilight", "Twilight Tide", "Dusky violet. Distinctive, premium evenings.", "#6A55C9"),
    Palette("slate", "Harbor Slate", "Cool graphite. Quiet, lets the data lead.", "#5A6B7D"),
)
BY_KEY = {p.key: p for p in PALETTES}
DEFAULT = "lagoon"
DEFAULT_CUSTOM = "#1B6C80"

# The hand-tuned Lagoon values, for previews only (the stylesheet has them).
LAGOON = {
    "light": {"--g": "#F1EEE7", "--sheet": "#FFFFFF", "--a": "#1B6C80", "--as": "#E3F1F3",
              "--b": "#E3DED3", "--mast-a": "#0E2A3B", "--mast-b": "#11425A",
              "--mast-c": "#17677D", "--hero-a": "#0E3B4C", "--hero-c": "#1E8296"},
    "dark": {"--g": "#0A1922", "--sheet": "#0F2230", "--a": "#57C3D1", "--as": "#123744",
             "--b": "#1E3646", "--mast-a": "#051219", "--mast-b": "#0A2533",
             "--mast-c": "#0E3A49", "--hero-a": "#082834", "--hero-c": "#14657A"},
}

_HEX = re.compile(r"#?([0-9a-fA-F]{6})")


def clean_hex(value) -> str | None:
    """'#1b6c80' or '1B6C80' -> '#1B6C80'; anything else -> None."""
    m = _HEX.fullmatch((value or "").strip())
    return "#" + m.group(1).upper() if m else None


def _rgb(h):
    n = int(h[1:], 16)
    return (n >> 16 & 255, n >> 8 & 255, n & 255)


def _hex(c):
    return "#" + "".join(f"{round(max(0, min(255, v))):02X}" for v in c)


def mix(a: str, b: str, w: float) -> str:
    """w of a, 1-w of b."""
    return _hex(x * w + y * (1 - w) for x, y in zip(_rgb(a), _rgb(b)))


def _lum(h):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def toward(c: str, end: str, bg: str, r: float) -> str:
    """The least change from c toward end that reaches contrast r against bg."""
    for i in range(51):
        m = mix(c, end, 1 - i * 0.02)
        if ratio(m, bg) >= r:
            return m
    return end


def _dk(c):
    return toward(c, "#000000", "#FFFFFF", 6)


def derive(b: str, mode: str) -> dict[str, str]:
    """Tokens for one mode from base colour b (already clean)."""
    if mode == "dark":
        sheet = mix(b, "#0C1419", 0.10)
        # A near-white base makes the tint too pale for light text on it;
        # deepen it only when needed (normal bases pass unchanged).
        tint = toward(mix(b, sheet, 0.24), "#000000", "#FFFFFF", 7.2)
        return {
            "--g": mix(b, "#060B0F", 0.08), "--sheet": sheet, "--r": mix(b, "#121C23", 0.12),
            "--act": mix(b, "#16222B", 0.18), "--b": mix(b, "#1A2730", 0.16),
            "--rule": mix(b, "#141F27", 0.12), "--ctl": mix(b, "#3A4A55", 0.2),
            "--a": toward(b, "#FFFFFF", sheet, 4.6), "--link": toward(b, "#FFFFFF", sheet, 5.5),
            "--as": tint, "--ai": toward(b, "#FFFFFF", tint, 7), "--band": mix(b, sheet, 0.36),
            "--next": toward(b, "#FFFFFF", sheet, 4.6), "--nexti": "#061016",
            "--mast-a": _dk(mix(b, "#020507", 0.10)), "--mast-b": _dk(mix(b, "#020507", 0.22)),
            "--mast-c": _dk(mix(b, "#020507", 0.36)), "--hero-a": _dk(mix(b, "#020507", 0.28)),
            "--hero-b": _dk(mix(b, "#020507", 0.42)), "--hero-c": _dk(mix(b, "#020507", 0.56)),
            "--glow": mix(b, "#FFFFFF", 0.55),
        }
    tint = mix(b, "#FFFFFF", 0.12)
    ink = toward(mix(b, "#0A0F14", 0.18), "#000000", "#FFFFFF", 13)
    return {
        "--a": toward(b, "#000000", "#FFFFFF", 4.6), "--link": toward(b, "#000000", "#FFFFFF", 5.2),
        "--as": tint, "--ai": toward(b, "#000000", tint, 7), "--band": mix(b, "#FFFFFF", 0.22),
        "--next": toward(b, "#000000", "#FFFFFF", 4.6), "--t": ink, "--pri": ink, "--done": ink,
        "--mast-a": _dk(mix(b, "#06121A", 0.18)), "--mast-b": _dk(mix(b, "#06121A", 0.38)),
        "--mast-c": _dk(mix(b, "#06121A", 0.60)), "--hero-a": _dk(mix(b, "#06121A", 0.36)),
        "--hero-b": _dk(mix(b, "#06121A", 0.56)), "--hero-c": _dk(mix(b, "#06121A", 0.78)),
        "--glow": mix(b, "#FFFFFF", 0.6),
    }


def base_for(key: str, custom: str | None) -> str | None:
    """The base colour a palette choice resolves to; None for Lagoon."""
    if key == "custom":
        return clean_hex(custom)
    if key in BY_KEY and key != DEFAULT:
        return BY_KEY[key].base
    return None


@lru_cache(maxsize=32)
def css(key: str, custom: str | None = None) -> str:
    """The <style> body that applies a palette, or "" for Lagoon. Selectors
    match the stylesheet's own (`:root:not(.dark)` / `:root.dark`) so the
    later block wins by order, not specificity."""
    base = base_for(key, custom)
    if not base:
        return ""
    out = []
    for sel, mode in ((":root:not(.dark)", "light"), (":root.dark", "dark")):
        body = ";".join(f"{k}:{v}" for k, v in derive(base, mode).items())
        out.append(f"{sel}{{{body}}}")
    return "".join(out)


def preview(key: str, base: str, mode: str = "light") -> dict[str, str]:
    """Tokens for a palette card's mini-UI drawing."""
    if key == DEFAULT:
        return LAGOON[mode]
    neutral = {} if mode == "dark" else {"--g": "#F1EEE7", "--sheet": "#FFFFFF", "--b": "#E3DED3"}
    return {**neutral, **derive(base, mode)}

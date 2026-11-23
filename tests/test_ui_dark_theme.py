"""The fleet UI follows the system colour scheme (`prefers-color-scheme: dark`).

Every colour of the three UI stylesheets is a custom property declared in the
`:root` block; the dark scheme only re-declares those properties inside one
`@media (prefers-color-scheme: dark)` block. That is what these tests pin down:
no colour literal may sit in a rule (it would stay light in the dark scheme),
every light token has a dark value, text keeps its contrast, and every page
shell tells the browser that it supports both schemes (native controls,
scrollbars and the canvas colour follow it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "fleet"
STATIC = UI_DIR / "static" / "ui"
TEMPLATES = UI_DIR / "templates" / "ui"
STYLESHEETS = ("fleet-ui.css", "auth.css", "legacy.css")

DARK_MEDIA = re.compile(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{")
ROOT_BLOCK = re.compile(r":root\s*\{([^{}]*)\}")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
TOKEN = re.compile(r"(--[\w-]+)\s*:\s*([^;}]+)")


def _css(name: str) -> str:
    text = (STATIC / name).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _split(css: str) -> tuple[str, str]:
    """Return (light part, dark media block body)."""
    match = DARK_MEDIA.search(css)
    assert match, "no @media (prefers-color-scheme: dark) block"
    depth, index = 1, match.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(css[index], 0)
        index += 1
    return css[: match.start()] + css[index:], css[match.end() : index - 1]


def _tokens(block_source: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for root in ROOT_BLOCK.findall(block_source):
        tokens.update({k: v.strip() for k, v in TOKEN.findall(root)})
    return tokens


def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("name", STYLESHEETS)
def test_stylesheet_has_a_dark_scheme_and_declares_color_scheme(name: str) -> None:
    css = _css(name)
    light, dark = _split(css)
    assert re.search(r"color-scheme\s*:\s*light dark", light), f"{name}: :root lacks color-scheme"
    assert _tokens(dark), f"{name}: the dark block re-declares no token"


@pytest.mark.parametrize("name", STYLESHEETS)
def test_no_colour_literal_outside_the_token_blocks(name: str) -> None:
    """A literal in a rule would stay light in the dark scheme."""
    light, dark = _split(_css(name))
    rules = ROOT_BLOCK.sub("", light) + ROOT_BLOCK.sub("", dark)
    assert HEX.findall(rules) == []


@pytest.mark.parametrize("name", STYLESHEETS)
def test_every_light_token_has_a_dark_value(name: str) -> None:
    light, dark = _split(_css(name))
    light_tokens = {k for k, v in _tokens(light).items() if HEX.search(v)}
    assert light_tokens, f"{name}: no colour tokens"
    assert light_tokens - set(_tokens(dark)) == set()


# (foreground token, background token): text on the surfaces it is used on.
PAIRS = (
    ("--ink", "--bg"),
    ("--ink", "--paper"),
    ("--muted", "--paper"),
    ("--muted", "--bg"),
    ("--green", "--paper"),
    ("--red", "--redbg"),
    ("--amber", "--amberbg"),
    ("--green", "--mint"),
)


@pytest.mark.parametrize(("foreground", "background"), PAIRS)
def test_dark_text_keeps_its_contrast(foreground: str, background: str) -> None:
    light, dark = _split(_css("fleet-ui.css"))
    light_tokens, dark_tokens = _tokens(light), _tokens(dark)
    light_ratio = _contrast(light_tokens[foreground], light_tokens[background])
    dark_ratio = _contrast(dark_tokens[foreground], dark_tokens[background])
    # AA for body text, but never worse than the light scheme already is.
    assert dark_ratio >= min(4.5, light_ratio)


def test_dark_surface_is_actually_dark() -> None:
    _, dark = _split(_css("fleet-ui.css"))
    tokens = _tokens(dark)
    assert _luminance(tokens["--bg"]) < 0.05
    assert _luminance(tokens["--ink"]) > 0.4


@pytest.mark.parametrize("page", ["base.html", "login.html", "invite_base.html"])
def test_page_shell_declares_both_colour_schemes(page: str) -> None:
    html = (TEMPLATES / page).read_text(encoding="utf-8")
    assert '<meta name="color-scheme" content="light dark">' in html


def test_inline_svg_illustration_is_adapted_in_the_dark_scheme() -> None:
    """The login illustration carries its colours as SVG attributes, which a
    token cannot reach; the dark block has to handle it explicitly."""
    _, dark = _split(_css("auth.css"))
    assert re.search(r"\.illustration[^{]*\{[^}]*filter", dark)

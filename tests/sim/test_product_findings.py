"""Static-analysis regression tests for product bugs the simulation found.

Each test pins a product bug the simulation found (originally `xfail(strict=True)`; the
markers were removed once findings 40, 41 and 42 were fixed, so a regression fails loudly).
Nothing here runs the product; it only reads source and recipe text.
"""

import re
from pathlib import Path

import pytest

from agent.commands_channel import SSE_READ_TIMEOUT_S
from protocol.commands import COMMANDS_SSE_PING_INTERVAL_S

ROOT = Path(__file__).resolve().parents[2]


def _heredoc_after(text: str, anchor: str) -> str:
    """Body of the first `<<'EOF' ... EOF` here-document after `anchor`."""
    start = text.index(anchor)
    opening = re.compile(r"<<'?(\w+)'?\n").search(text, start)
    assert opening, "no here-document after the anchor"
    end = text.index(f"\n{opening.group(1)}\n", opening.end())
    return text[opening.end() : end]


def test_agent_sse_timeout_exceeds_the_fleet_ping_interval() -> None:
    # finding 42: the idle command stream must not time out between fleet pings.
    assert SSE_READ_TIMEOUT_S > COMMANDS_SSE_PING_INTERVAL_S


def test_watchdog_unit_user_is_created_by_the_image_recipe() -> None:
    unit = (ROOT / "watchdog" / "thermoctl-watchdog.service").read_text(encoding="utf-8")
    user_match = re.search(r"^User=(\S+)", unit, re.M)
    assert user_match, "the unit names no User="
    user = re.escape(user_match.group(1))
    creation = re.compile(
        rf"(useradd|adduser|sysusers|^\s*u\s+{user}\b).*\b{user}\b|^\s*u\s+{user}\b",
        re.M,
    )
    for path in (ROOT / "image").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if creation.search(text):
                return
    pytest.fail(f"no useradd/adduser/sysusers entry for {user_match.group(1)} under image/")


def test_install_sh_preset_state_file_uses_the_keys_the_watchdog_parses() -> None:
    install = (ROOT / "image" / "common" / "install.sh").read_text(encoding="utf-8")
    body = _heredoc_after(install, 'STATE_FILE="$(root_path /var/lib/thermoctl-watchdog)')
    written = set(re.findall(r"^([A-Za-z_]+)=", body, re.M))
    go = (ROOT / "watchdog" / "state.go").read_text(encoding="utf-8")
    parsed = set(re.findall(r'values\["(\w+)"\]', go))
    assert "desired" in parsed, "state.go no longer reads a 'desired' key"
    assert written, "install.sh writes no keys into the pre-set state file"
    assert "desired" in written and written <= parsed, (
        f"install.sh writes {sorted(written)}, state.go parses {sorted(parsed)}"
    )


def test_heredoc_helper_extracts_the_body() -> None:
    text = "X=1\ncat >f <<'EOF'\n# c\nA=1\nEOF\nrest\n"
    assert _heredoc_after(text, "X=1") == "# c\nA=1"

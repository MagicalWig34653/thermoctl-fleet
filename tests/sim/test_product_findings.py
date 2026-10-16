"""Static-analysis regression tests for product bugs the simulation found.

Each test is `xfail(strict=True)`: it documents the bug today and fails loudly (XPASS ->
failure) the moment the product is fixed, so the finding file and this test get retired
together. Nothing here runs the product; it only reads source and recipe text.
"""

import inspect
import re
from pathlib import Path

import pytest

from agent.transport import build_client

ROOT = Path(__file__).resolve().parents[2]


def _heredoc_after(text: str, anchor: str) -> str:
    """Body of the first `<<'EOF' ... EOF` here-document after `anchor`."""
    start = text.index(anchor)
    opening = re.compile(r"<<'?(\w+)'?\n").search(text, start)
    assert opening, "no here-document after the anchor"
    end = text.index(f"\n{opening.group(1)}\n", opening.end())
    return text[opening.end() : end]


@pytest.mark.xfail(
    strict=True,
    reason="finding 42: the agent's default client timeout (10 s) is shorter than the "
    "fleet's SSE ping interval (15 s), so an idle command stream always times out",
)
def test_agent_sse_timeout_exceeds_the_fleet_ping_interval():
    timeout = inspect.signature(build_client).parameters["timeout"].default
    source = (ROOT / "fleet" / "app.py").read_text(encoding="utf-8")
    match = re.search(r"^_DEFAULT_COMMANDS_SSE_PING_INTERVAL_S\s*=\s*([\d.]+)", source, re.M)
    assert match, "fleet/app.py no longer defines _DEFAULT_COMMANDS_SSE_PING_INTERVAL_S"
    ping = float(match.group(1))
    assert isinstance(timeout, float)
    assert timeout > ping, f"agent timeout {timeout}s <= fleet ping interval {ping}s"


@pytest.mark.xfail(
    strict=True,
    reason="finding 40: thermoctl-watchdog.service runs as User=thermoctl-watchdog, "
    "which nothing under image/ creates",
)
def test_watchdog_unit_user_is_created_by_the_image_recipe():
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


@pytest.mark.xfail(
    strict=True,
    reason="finding 41: the state file install.sh pre-sets uses keys (DIGEST/PROVEN) that "
    "watchdog/state.go does not parse",
)
def test_install_sh_preset_state_file_uses_the_keys_the_watchdog_parses():
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


def test_heredoc_helper_extracts_the_body():
    text = "X=1\ncat >f <<'EOF'\n# c\nA=1\nEOF\nrest\n"
    assert _heredoc_after(text, "X=1") == "# c\nA=1"

"""Tests for the vendored browser age implementation (P5.5b, owner
decision 2026-09-28: "no CDN at runtime ... sha256 recorded and verified
by a test").

The `node`-based interop test is skipped, with a reason, if `node` is not
on `PATH` -- this codebase's own testing method allows that only when the
tool genuinely is not installed, never as a way to avoid writing the real
test; the sha256/no-external-URL checks above do not need `node` at all
and always run.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pyrage
import pytest
from pyrage import x25519

from fleet.restore_vendor import AGE_VENDOR_JS_PATH, AGE_VENDOR_JS_SHA256


def test_vendored_age_js_exists() -> None:
    assert AGE_VENDOR_JS_PATH.is_file()


def test_vendored_age_js_matches_the_recorded_sha256() -> None:
    digest = hashlib.sha256(AGE_VENDOR_JS_PATH.read_bytes()).hexdigest()
    assert digest == AGE_VENDOR_JS_SHA256, (
        "The vendored age JS file's bytes no longer match "
        "fleet.restore_vendor.AGE_VENDOR_JS_SHA256 -- if this file was "
        "deliberately upgraded, update that constant (and this project's "
        "own record of the npm package/version) in the same change."
    )


def test_vendored_age_js_contains_no_external_url_references() -> None:
    """"No CDN at runtime" as a property of the file's own bytes, not only
    a claim in a docstring."""

    content = AGE_VENDOR_JS_PATH.read_text(encoding="utf-8")
    assert "http://" not in content
    assert "https://" not in content


def test_vendored_age_js_exposes_exactly_the_expected_global() -> None:
    content = AGE_VENDOR_JS_PATH.read_text(encoding="utf-8")
    assert "thermoctlAge" in content
    assert "encryptToRecipient" in content


_NODE_PATH = shutil.which("node")


@pytest.mark.skipif(_NODE_PATH is None, reason="node is not installed")
def test_vendored_age_js_encrypts_something_pyrage_can_decrypt(tmp_path: Path) -> None:
    """Runs the *actual* vendored bundle under `node` (never a
    reimplementation of it), encrypts a short string to a real
    `pyrage`-generated recipient, and decrypts the result with `pyrage` --
    real cross-implementation interop, no mock of either side."""

    identity = x25519.Identity.generate()
    recipient = str(identity.to_public())
    plaintext = "a landlord's restore key, for this test only"

    script = tmp_path / "run.mjs"
    script.write_text(
        f"""
import {{ readFileSync, writeFileSync }} from "node:fs";
globalThis.window = globalThis;
globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
const code = readFileSync({str(AGE_VENDOR_JS_PATH)!r}, "utf8");
(0, eval)(code);
const bytes = new TextEncoder().encode({plaintext!r});
window.thermoctlAge.encryptToRecipient(bytes, {recipient!r}).then((ciphertext) => {{
    writeFileSync({str(tmp_path / "out.bin")!r}, Buffer.from(ciphertext));
}});
""",
        encoding="utf-8",
    )
    assert _NODE_PATH is not None  # narrows the type; the skipif above already checked this.
    subprocess.run(  # noqa: S603 -- a fixed, resolved (not PATH-searched-at-call-time) `node`
        # binary, given only this test's own local, non-attacker-controlled script path -- no
        # untrusted input reaches this call.
        [_NODE_PATH, str(script)], check=True, capture_output=True, text=True, timeout=30
    )
    ciphertext = (tmp_path / "out.bin").read_bytes()
    decrypted = pyrage.decrypt(ciphertext, [identity])
    assert decrypted.decode("utf-8") == plaintext

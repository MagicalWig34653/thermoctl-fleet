from pathlib import Path

import pytest

from tools.sim import browse
from tools.sim.http import confine_to_cwd

LOCAL = frozenset({"127.0.0.1", "localhost"})


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("SIM_ALLOWED_HOSTS", raising=False)
    return work


def test_shot_defaults(cwd: Path):
    a = browse.build_cli_args(
        ["shot", "--url", "http://127.0.0.1:8200/", "--out", "a.png", "--width", "390"]
    )
    assert a.command == "shot" and a.width == 390 and a.dark is False
    assert a.out == cwd.resolve() / "a.png" and a.role is None


def test_shot_flags(cwd: Path):
    a = browse.build_cli_args(
        ["shot", "--url", "http://localhost/x", "--out", "b.png", "--dark", "--role", "landlord"]
    )
    assert a.dark is True and a.role == "landlord" and a.width == 390


def test_text_needs_no_out(cwd: Path):
    a = browse.build_cli_args(["text", "--url", "http://127.0.0.1:1/", "--role", "tenant_1"])
    assert (a.command, a.out, a.role) == ("text", None, "tenant_1")


def test_non_local_url_refused(cwd: Path):
    with pytest.raises(ValueError):
        browse.build_cli_args(["shot", "--url", "https://example.com", "--out", "a.png"])
    with pytest.raises(ValueError):
        browse.build_cli_args(["text", "--url", "file:///etc/passwd"])
    with pytest.raises(ValueError):
        browse.build_cli_args(["text", "--url", "http://u:p@127.0.0.1/"])


def test_env_allowed_hosts_extend(cwd: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIM_ALLOWED_HOSTS", "fleet.test")
    assert browse.build_cli_args(["text", "--url", "https://fleet.test/"]).url
    with pytest.raises(ValueError):
        browse.build_cli_args(["text", "--url", "http://127.0.0.1/"])


def test_out_must_be_inside_cwd(cwd: Path):
    for bad in ("../a.png", "/etc/a.png", "sub/../../a.png"):
        with pytest.raises(ValueError):
            browse.build_cli_args(["shot", "--url", "http://127.0.0.1/", "--out", bad])


def test_out_symlink_escape_refused(cwd: Path, tmp_path: Path):
    (cwd / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        browse.build_cli_args(["shot", "--url", "http://127.0.0.1/", "--out", "link/a.png"])


def test_confine_to_cwd_accepts_inside(cwd: Path):
    assert confine_to_cwd(Path("x/y.png")) == cwd.resolve() / "x" / "y.png"


@pytest.mark.parametrize("role", ["", "Landlord", "../x", "a/b", "a b", "ro.le", "x\n"])
def test_bad_roles_refused(cwd: Path, role: str):
    with pytest.raises(ValueError):
        browse.build_cli_args(["text", "--url", "http://127.0.0.1/", "--role", role])
    with pytest.raises(ValueError):
        browse.validate_role(role)


def test_bad_width_and_unknown_args(cwd: Path):
    for extra in (["--width", "5"], ["--width", "99999"], ["--width", "abc"]):
        with pytest.raises(ValueError):
            browse.build_cli_args(["shot", "--url", "http://127.0.0.1/", "--out", "a.png", *extra])
    with pytest.raises(ValueError):
        browse.build_cli_args(["text", "--url", "http://127.0.0.1/", "--out", "a.png"])
    with pytest.raises(ValueError):
        browse.build_cli_args([])


def test_request_allowed_predicate():
    assert browse.request_allowed("http://127.0.0.1:8200/app.js", LOCAL)
    assert browse.request_allowed("http://localhost/x", LOCAL)
    assert browse.request_allowed("data:image/png;base64,AAAA", LOCAL)
    assert not browse.request_allowed("https://cdn.example.com/lib.js", LOCAL)
    assert not browse.request_allowed("ftp://127.0.0.1/x", LOCAL)
    assert not browse.request_allowed("http://127.0.0.1.evil.test/", LOCAL)
    assert not browse.request_allowed("http://u@127.0.0.1/", LOCAL)


def test_storage_state_path(tmp_path: Path):
    (tmp_path / "landlord.json").write_text("{}")
    env = {"SIM_STORAGE_STATE_DIR": str(tmp_path)}
    assert browse.storage_state_path("landlord", env) == tmp_path / "landlord.json"
    assert browse.storage_state_path("tenant", env) is None  # missing = anonymous
    assert browse.storage_state_path(None, env) is None
    assert browse.storage_state_path("landlord", {}) is None
    with pytest.raises(ValueError):
        browse.storage_state_path("../landlord", env)


def test_main_refusal_exits_2(cwd: Path, capsys: pytest.CaptureFixture[str]):
    assert browse.main(["text", "--url", "https://example.com"]) == 2
    assert "refused" in capsys.readouterr().err


def test_main_prints_browse_result(cwd: Path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    seen = {}

    def fake(args, allowed):  # type: ignore[no-untyped-def]
        seen["args"] = args
        return "title: T\n\nbody"

    monkeypatch.setattr(browse, "_browse", fake)
    assert browse.main(["text", "--url", "http://127.0.0.1:1/"]) == 0
    assert capsys.readouterr().out == "title: T\n\nbody\n"
    assert seen["args"].command == "text"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("ws://127.0.0.1:8200/x", True),
        ("wss://localhost/x", True),
        ("wss://evil.com/", False),
        ("ws://127.0.0.1.evil.test/", False),
        ("ws://u@127.0.0.1/x", False),
        ("ws://u:p@127.0.0.1/x", False),
        ("http://127.0.0.1/x", False),
        ("ftp://127.0.0.1/x", False),
    ],
)
def test_websocket_allowed(url: str, ok: bool):
    assert browse.websocket_allowed(url, LOCAL) is ok


def test_websocket_allowed_respects_env_hosts():
    assert browse.websocket_allowed("wss://fleet.test/ws", frozenset({"fleet.test"}))
    assert not browse.websocket_allowed("ws://127.0.0.1/", frozenset({"fleet.test"}))


def test_anonymous_note():
    assert browse.anonymous_note("landlord", None) == (
        "note: running anonymous (no stored login for role landlord)"
    )
    assert browse.anonymous_note(None, None) is None
    assert browse.anonymous_note("landlord", Path("/x/landlord.json")) is None


def test_format_links_lists_targets_once_and_skips_noise():
    raw = [
        ("Ansehen", "http://127.0.0.1:8200/ui/apartments/sim-a01"),
        ("Ansehen", "http://127.0.0.1:8200/ui/apartments/sim-a02"),
        ("Ansehen", "http://127.0.0.1:8200/ui/apartments/sim-a01"),  # duplicate
        ("", "http://127.0.0.1:8200/ui/tasks"),
        ("Hoch", "http://127.0.0.1:8200/ui/#main"),
        ("x", "javascript:alert(1)"),
        ("Hilfe", "https://example.com/help"),
    ]
    text = browse.format_links(raw, "http://127.0.0.1:8200/ui/", {"127.0.0.1"})
    lines = text.splitlines()
    assert lines[0] == "links:"
    assert "- Ansehen -> /ui/apartments/sim-a01" in lines
    assert "- Ansehen -> /ui/apartments/sim-a02" in lines
    assert "- (no text) -> /ui/tasks" in lines
    assert sum("sim-a01" in line for line in lines) == 1
    assert not any("javascript" in line or "#main" in line for line in lines)
    assert "- Hilfe -> https://example.com/help (external)" in lines


def test_format_links_empty_and_capped():
    assert browse.format_links([], "http://127.0.0.1/", {"127.0.0.1"}) == "links: none"
    many = [(f"n{i}", f"http://127.0.0.1/p{i}") for i in range(200)]
    out = browse.format_links(many, "http://127.0.0.1/", {"127.0.0.1"})
    assert len(out.splitlines()) == browse.MAX_LINKS + 1

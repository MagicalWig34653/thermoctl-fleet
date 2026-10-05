"""Local development state and launcher behavior."""

from __future__ import annotations

import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from argon2 import PasswordHasher

from fleet.storage import create_storage
from fleet.totp_crypto import decrypt_totp_secret, load_totp_key
from tools import dev_fleet


def test_first_start_creates_private_state_and_real_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_fleet, "REPO_ROOT", tmp_path)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))

    assert dev_fleet.main(["--port", "8123", "--no-reload"]) == 0
    state = tmp_path / ".dev"
    key_file = state / "totp.key"
    login_file = state / "demo-login.txt"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600
    assert (state / "fleet-dev.db").is_file()
    assert (state / "backups").is_dir()
    assert (state / "diagnostic-bundles").is_dir()

    login = login_file.read_text()
    username = login.split("Username: ", 1)[1].splitlines()[0]
    password = login.split("Password: ", 1)[1].splitlines()[0]
    uri = login.split("TOTP provisioning URI: ", 1)[1].strip()
    storage = create_storage(f"sqlite:///{state / 'fleet-dev.db'}")
    assert len(storage.list_properties()) == 1
    assert len(storage.list_apartments()) == 3
    user = storage.get_ui_user_by_username(username)
    assert user is not None
    assert PasswordHasher().verify(user.password_hash, password)
    secret = parse_qs(urlparse(uri).query)["secret"][0]
    key = load_totp_key(key_file.read_text().strip())
    assert decrypt_totp_secret(user.totp_secret, user.id, key) == secret
    assert capsys.readouterr().out == login
    assert key_file.read_text().strip() not in login
    assert calls == [{"host": "127.0.0.1", "port": 8123, "reload": False, "reload_dirs": None}]


def test_second_start_reuses_credentials_without_duplicate_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_fleet, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert dev_fleet.main([]) == 0
    first = capsys.readouterr()
    state = tmp_path / ".dev"
    key = (state / "totp.key").read_bytes()
    login = (state / "demo-login.txt").read_bytes()
    db = (state / "fleet-dev.db").read_bytes()
    assert dev_fleet.main([]) == 0
    second = capsys.readouterr()
    assert first.out == second.out == login.decode()
    assert first.err == second.err == ""
    assert first.out.count("Password:") == second.out.count("Password:") == 1
    assert key == (state / "totp.key").read_bytes()
    assert db == (state / "fleet-dev.db").read_bytes()
    assert login == (state / "demo-login.txt").read_bytes()


def test_reset_requires_confirmation_and_replaces_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dev_fleet, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert dev_fleet.main([]) == 0
    capsys.readouterr()
    state = tmp_path / ".dev"
    old_key = (state / "totp.key").read_bytes()
    old_login = (state / "demo-login.txt").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert dev_fleet.main(["--reset"]) == 1
    assert (state / "totp.key").read_bytes() == old_key
    assert dev_fleet.main(["--reset", "--yes"]) == 0
    assert (state / "totp.key").read_bytes() != old_key
    assert (state / "demo-login.txt").read_bytes() != old_login


def test_private_writer_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "private"
    dev_fleet._write_private(path, "first")
    with pytest.raises(FileExistsError):
        dev_fleet._write_private(path, "second")
    assert path.read_text() == "first"




@pytest.mark.parametrize("port", ["0", "70000"])
def test_main_rejects_a_port_outside_the_valid_range(port: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        dev_fleet.main(["--port", port])
    assert excinfo.value.code == 2

import base64
import json
import os
import stat

import pytest

from tools.sim.env import logins

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_rfc_6238_sha1_vectors(moment, expected):
    assert logins.totp(RFC_SECRET, moment, digits=8) == expected


def test_totp_six_digits_is_the_tail_of_the_eight_digit_code():
    assert logins.totp(RFC_SECRET, 59) == "287082"


def test_totp_tolerates_lowercase_spaces_and_missing_padding():
    spaced = " ".join(RFC_SECRET.rstrip("=").lower()[i : i + 4] for i in range(0, 32, 4))
    assert logins.totp(spaced, 59) == "287082"


def test_totp_changes_only_at_step_boundaries():
    assert logins.totp(RFC_SECRET, 30) == logins.totp(RFC_SECRET, 59)
    assert logins.totp(RFC_SECRET, 59) != logins.totp(RFC_SECRET, 60)


def test_role_and_paths(tmp_path):
    assert logins.role_of("sim-landlord") == "landlord"
    assert logins.role_of("plain") == "plain"
    assert logins.storage_path(tmp_path, "sim-admin") == tmp_path / "storage-states" / "admin.json"
    assert logins.login_url("http://127.0.0.1:8200/") == "http://127.0.0.1:8200/ui/login"


def test_form_fields_use_the_real_element_ids():
    fields = logins.form_fields("sim-admin", {"password": "pw", "totp_secret": RFC_SECRET}, 59)
    assert fields == {"username": "sim-admin", "password": "pw", "totp_code": "287082"}


def test_form_ids_exist_in_the_fleet_login_template():
    from tools.sim.env.config import ROOT

    html = (ROOT / "fleet" / "templates" / "ui" / "login.html").read_text(encoding="utf-8")
    for element_id in ("username", "password", "totp_code", "login-form"):
        assert f'id="{element_id}"' in html
    assert 'action="/ui/login"' in html


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://h:1/ui/", True),
        ("http://h:1/ui/inventory", True),
        ("http://h:1/ui/login", False),
        ("http://h:1/ui/login?next=/ui/", False),
        ("http://elsewhere/ui/", False),
    ],
)
def test_login_succeeded(url, ok):
    assert logins.login_succeeded(url, "http://h:1") is ok


def test_load_users_reads_and_validates(tmp_path):
    (tmp_path / "users.json").write_text(
        json.dumps({"sim-admin": {"password": "p", "totp_secret": "S"}})
    )
    assert logins.load_users(tmp_path) == {"sim-admin": {"password": "p", "totp_secret": "S"}}


@pytest.mark.parametrize("content", ["{broken", "[1]", '{"u": {"password": "p"}}'])
def test_load_users_rejects_bad_files(tmp_path, content):
    (tmp_path / "users.json").write_text(content)
    with pytest.raises(RuntimeError, match="users.json"):
        logins.load_users(tmp_path)


def test_load_users_missing_file_points_at_up_fleet(tmp_path):
    with pytest.raises(RuntimeError, match="up_fleet"):
        logins.load_users(tmp_path)


def test_write_storage_state_is_private_even_over_an_existing_wide_file(tmp_path):
    path = tmp_path / "storage-states" / "admin.json"
    logins.write_storage_state(path, {"cookies": [{"name": "sid"}]})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"cookies": [{"name": "sid"}]}
    logins.write_storage_state(path, {"cookies": []})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["admin.json"]


def test_build_storage_states_logs_in_every_user_at_virtual_time(tmp_path, monkeypatch):
    (tmp_path / "users.json").write_text(
        json.dumps({"sim-admin": {"password": "p", "totp_secret": RFC_SECRET}})
    )
    seen = {}

    def fake(directory, base_url, users, now):
        seen.update(directory=directory, base_url=base_url, users=users, now=now)
        return {"admin": True}

    monkeypatch.setattr(logins, "_login_all", fake)
    assert logins.build_storage_states(tmp_path, "http://h:1") == {"admin": True}
    assert seen["base_url"] == "http://h:1" and "sim-admin" in seen["users"]
    assert seen["now"].year == 2026  # SIM_START: no clock file yet


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logins, "state_dir", lambda: tmp_path)
    assert logins.main(["--refresh"]) == 1  # no users.json
    assert "error:" in capsys.readouterr().err
    monkeypatch.setattr(logins, "build_storage_states", lambda d, u: {"a": True, "b": False})
    assert logins.main(["--refresh"]) == 1
    assert "login b: FAILED" in capsys.readouterr().out
    monkeypatch.setattr(logins, "build_storage_states", lambda d, u: {"a": True})
    assert logins.main(["--refresh"]) == 0


def test_main_requires_refresh_flag():
    with pytest.raises(SystemExit):
        logins.main([])

import base64
import json
import secrets
import stat
from datetime import UTC, datetime

import pytest

from fleet.storage import create_storage
from protocol.registration import AgentRegistrationFile
from tools.sim.env.config import APARTMENT_IDS, UI_USERS
from tools.sim.env.fleet_setup import create_sim_users, seed_fleet, virtual_now


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    state = tmp_path_factory.mktemp("state")
    url = f"sqlite:///{state / 'fleet.db'}"
    (state / "clock").write_text("2026-02-02 08:00:00\n")
    result = seed_fleet(state, url, fleet_address="http://127.0.0.1:8200")
    return state, url, result


def test_virtual_now_reads_clock_or_defaults(tmp_path):
    assert virtual_now(tmp_path) == datetime(2026, 1, 5, 8, tzinfo=UTC)
    (tmp_path / "clock").write_text("2026-02-02 09:30:00\n")
    assert virtual_now(tmp_path) == datetime(2026, 2, 2, 9, 30, tzinfo=UTC)


def test_three_properties_twelve_apartments(seeded):
    _, url, result = seeded
    storage = create_storage(url)
    props = storage.list_properties()
    assert len(props) == 3
    apts = storage.list_apartments()
    assert sorted(a.id for a in apts) == list(APARTMENT_IDS)
    for prop in props:
        assert len(storage.list_apartments_by_property(prop.id)) == 4
    assert all(a.label and a.floor for a in apts)
    assert len({a.label for a in apts}) == 12
    assert result.apartments == list(APARTMENT_IDS)


def test_one_prepared_device_per_apartment_with_unique_codes(seeded):
    _, url, result = seeded
    devices = create_storage(url).list_devices()
    assert len(devices) == 12
    assert {d.state for d in devices} == {"prepared"}
    assert len(set(result.codes.values())) == 12
    assert set(result.codes) == set(APARTMENT_IDS)
    assert set(result.device_ids) == set(APARTMENT_IDS)


def test_registration_files_are_private_and_valid(seeded):
    state, _, result = seeded
    for apt in APARTMENT_IDS:
        path = state / "registrations" / f"{apt}.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        reg = AgentRegistrationFile.model_validate_json(path.read_text())
        assert reg.registration_code == result.codes[apt]
        assert reg.fleet_address == "http://127.0.0.1:8200"
    index = state / "registrations" / "_index.json"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert json.loads(index.read_text())["sim-a01"]["device_id"] == result.device_ids["sim-a01"]


def test_seed_twice_is_refused(seeded):
    state, url, _ = seeded
    with pytest.raises(RuntimeError, match="already seeded"):
        seed_fleet(state, url, fleet_address="http://127.0.0.1:8200")


def test_users_are_created_with_runtime_secrets(tmp_path):
    from fleet.storage import upgrade

    url = f"sqlite:///{tmp_path / 'f.db'}"
    upgrade(url)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    users = create_sim_users(tmp_path, url, totp_key=key)
    assert set(users) == set(UI_USERS)
    path = tmp_path / "users.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert set(stored) == set(UI_USERS)
    passwords = {u["password"] for u in stored.values()}
    assert len(passwords) == 3 and all(len(p) >= 24 for p in passwords)
    assert all(u["totp_secret"] for u in stored.values())
    assert {u.username for u in create_storage(url).list_ui_users()} == set(UI_USERS)

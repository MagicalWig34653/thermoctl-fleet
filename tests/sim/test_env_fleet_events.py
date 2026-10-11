"""Fleet-side event handlers (IMAGE_UPDATE, BACKUP, TOKEN_EXPIRY, TENANT_CHANGE)
against the real Storage / FastAPI app, no Docker."""

import hashlib
import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet.backup_storage import BackupBlobStorage, get_backup_storage
from fleet.storage import TenantChangeOutcome, create_storage, get_storage
from protocol.backups import BackupKind
from protocol.desired_state import DesiredState
from protocol.heartbeat import Heartbeat
from tools.sim.clock import clock_path
from tools.sim.env import HANDLERS, apply_event_with, fleet_events
from tools.sim.env.enroll import CONFIRM_USER, enroll_container_apartments
from tools.sim.env.fleet_setup import seed_fleet
from tools.sim.scenario import Event, EventKind
from tools.sim.sim_agent import SimAgent, build_backup_body, healthy_sections

NOW = datetime(2026, 2, 2, 8, tzinfo=UTC)
APT = "sim-a05"


def _ev(kind, apt=APT, n="0"):
    return Event(f"{kind}:{apt}:{n}", 1, kind, apt)


def _apply(event):
    apply_event_with(event, lambda cmd: pytest.fail(f"unexpected docker call {cmd}"))


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    clock_path(tmp_path).parent.mkdir()
    clock_path(tmp_path).write_text("2026-02-02 08:00:00\n")
    (tmp_path / "fleet-data").mkdir()
    url = f"sqlite:///{tmp_path / 'fleet-data' / 'fleet.db'}"
    seed_fleet(tmp_path, url)
    tokens = enroll_container_apartments(tmp_path, url, NOW)
    return tmp_path, create_storage(url), tokens


@pytest.fixture
def client(fleet):
    from fleet.app import app

    tmp, storage, _ = fleet
    blobs = BackupBlobStorage(tmp / "backups")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_backup_storage] = lambda: blobs
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_backup_storage, None)


def _agent(client, fleet, apt=APT):
    tmp, _, tokens = fleet
    token = tokens[apt].read_text().strip()
    return SimAgent(
        apt,
        client,  # type: ignore[arg-type]
        token,
        healthy_sections,
        token_file=tokens[apt],
        state_dir=tmp,
    )


# --- IMAGE_UPDATE ---


def test_image_update_sets_a_new_desired_state_revision(fleet):
    tmp, storage, _ = fleet
    assert storage.get_desired_state(APT) is None
    _apply(_ev(EventKind.IMAGE_UPDATE))
    record = storage.get_desired_state(APT)
    assert record is not None and record.revision == 1
    assert record.created_by == CONFIRM_USER and record.reason.startswith("simulation: ")
    state = DesiredState.model_validate_json(record.state_json)
    for name in ("thermoctl", "agent"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", getattr(state.services, name).digest)
    marker = json.loads((tmp / "faults" / f"{APT}.version").read_text())
    assert marker == {"revision": 1, "version": state.services.thermoctl.version}


def test_image_update_is_deterministic_and_idempotent(fleet):
    _, storage, _ = fleet
    event = _ev(EventKind.IMAGE_UPDATE)
    _apply(event)
    first = storage.get_desired_state(APT).state_json
    _apply(event)
    assert storage.get_desired_state(APT).revision == 1
    assert storage.get_desired_state(APT).state_json == first


def test_second_image_update_is_a_new_revision_keeping_other_services(fleet):
    _, storage, _ = fleet
    _apply(_ev(EventKind.IMAGE_UPDATE, n="0"))
    _apply(_ev(EventKind.IMAGE_UPDATE, n="1"))
    history = storage.desired_state_history(APT)
    assert [r.revision for r in history] == [2, 1]
    new, old = (DesiredState.model_validate_json(r.state_json) for r in history)
    assert new.services.thermoctl.digest != old.services.thermoctl.digest
    assert new.services.mosquitto == old.services.mosquitto
    assert new.window == old.window


def test_image_update_names_the_images_the_ui_names_not_a_free_source(fleet):
    from fleet.desired_state_sources import DISPLAY_SOURCES

    _, storage, _ = fleet
    _apply(_ev(EventKind.IMAGE_UPDATE))
    state = DesiredState.model_validate_json(storage.get_desired_state(APT).state_json)
    for name, source in DISPLAY_SOURCES.items():
        assert getattr(state.services, name).image == source


def test_image_update_also_applies_to_vm_apartments(fleet):
    _, storage, _ = fleet
    _apply(_ev(EventKind.IMAGE_UPDATE, apt="sim-a01"))
    assert storage.get_desired_state("sim-a01").revision == 1


def test_image_update_without_a_database_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="not found"):
        _apply(_ev(EventKind.IMAGE_UPDATE))


def test_apartment_ids_are_validated():
    with pytest.raises(ValueError):
        fleet_events.apartment_of(Event("x", 0, EventKind.BACKUP, "../etc"))
    with pytest.raises(ValueError):
        fleet_events.apartment_of(Event("x", 0, EventKind.BACKUP, "sim-a99"))


# --- BACKUP ---


def test_backup_marker_alternates_kinds_and_is_sized(fleet):
    tmp, _, _ = fleet
    kinds = []
    for n in range(4):
        _apply(_ev(EventKind.BACKUP, n=str(n)))
        marker = json.loads((tmp / "faults" / f"{APT}.backup").read_text())
        assert marker["id"] == f"backup:{APT}:{n}"
        assert 50_000 <= marker["size"] <= 500_000
        kinds.append(marker["kind"])
    assert kinds == ["operational_data", "device_config"] * 2


def test_backup_marker_is_deterministic(fleet):
    assert fleet_events.backup_marker(_ev(EventKind.BACKUP, n="3")) == fleet_events.backup_marker(
        _ev(EventKind.BACKUP, n="3")
    )


def test_backup_skips_vm_apartments(fleet):
    with pytest.raises(NotImplementedError, match="no container"):
        _apply(_ev(EventKind.BACKUP, apt="sim-a01"))


def test_agent_uploads_an_age_encrypted_operational_block(fleet, client):
    tmp, storage, _ = fleet
    agent = _agent(client, fleet)
    assert agent.tick() is True
    assert storage.list_backups_for_apartment(APT) == []
    _apply(_ev(EventKind.BACKUP, n="0"))
    marker = json.loads((tmp / "faults" / f"{APT}.backup").read_text())
    assert agent.tick() is True
    (row,) = storage.list_backups_for_apartment(APT)
    assert row.kind == "operational_data"
    assert 50_000 <= row.size_bytes <= 500_000 + 2_000
    blob = next((tmp / "backups").rglob("*"))
    while blob.is_dir():
        blob = next(blob.iterdir())
    data = blob.read_bytes()
    assert data.startswith(b"age-encryption.org/v1\n-> ")
    assert hashlib.sha256(data).hexdigest() == row.content_hash
    assert marker["size"] <= row.size_bytes
    assert APT.encode() not in data and b'"' not in data[:64]


def test_agent_uploads_a_plain_device_config_without_tenant_data(fleet, client):
    tmp, storage, _ = fleet
    agent = _agent(client, fleet)
    _apply(_ev(EventKind.BACKUP, n="1"))
    agent.tick()
    (row,) = storage.list_backups_for_apartment(APT)
    assert row.kind == "device_config"
    blob = next(p for p in (tmp / "backups").rglob("*") if p.is_file())
    config = json.loads(blob.read_bytes())
    assert config["apartment"] == APT and config["simulated"] is True
    assert set(config) == {"apartment", "services", "agent", "simulated"}


def test_a_marker_is_uploaded_once_and_not_again_after_a_restart(fleet, client):
    _, storage, _ = fleet
    agent = _agent(client, fleet)
    _apply(_ev(EventKind.BACKUP, n="0"))
    agent.tick()
    agent.tick()
    assert len(storage.list_backups_for_apartment(APT)) == 1
    _agent(client, fleet).tick()  # a restarted box adopts the marker silently
    assert len(storage.list_backups_for_apartment(APT)) == 1
    _apply(_ev(EventKind.BACKUP, n="2"))  # a new event is a new backup
    agent.tick()
    assert len(storage.list_backups_for_apartment(APT)) == 2


@pytest.mark.parametrize(
    "marker",
    [
        {"id": "a", "kind": "nope", "size": 1000},
        {"id": "a", "kind": "operational_data", "size": 0},
        {"id": "a", "kind": "operational_data", "size": 10**9},
        {"id": "a", "kind": "operational_data", "size": True},
    ],
)
def test_unusable_backup_markers_are_ignored(fleet, client, marker):
    tmp, storage, _ = fleet
    agent = _agent(client, fleet)
    fleet_events.write_marker(tmp, APT, "backup", marker)
    assert agent.tick() is True
    assert storage.list_backups_for_apartment(APT) == []


def test_upload_failure_never_breaks_the_heartbeat(fleet):
    tmp, _, tokens = fleet

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/backups":
            raise httpx.ConnectError("down")
        return httpx.Response(204)

    http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    agent = SimAgent(APT, http, "t", healthy_sections, state_dir=tmp)
    fleet_events.write_marker(tmp, APT, "backup", {"id": "a", "kind": "device_config", "size": 100})
    assert agent.tick() is True


def test_a_refused_upload_is_logged_not_raised(fleet, caplog):
    tmp, _, _ = fleet
    http = httpx.Client(
        base_url="http://x",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(422 if r.url.path == "/v1/backups" else 204)
        ),
    )
    agent = SimAgent(APT, http, "t", healthy_sections, state_dir=tmp)
    fleet_events.write_marker(tmp, APT, "backup", {"id": "a", "kind": "device_config", "size": 100})
    assert agent.tick() is True
    assert "refused: HTTP 422" in caplog.text


def test_operational_body_is_real_age_and_not_plaintext():
    one = build_backup_body(BackupKind.OPERATIONAL_DATA, APT, 60_000, "v")
    two = build_backup_body(BackupKind.OPERATIONAL_DATA, APT, 60_000, "v")
    assert one != two  # fresh random bytes and fresh recipients every time
    assert one.startswith(b"age-encryption.org/v1\n-> X25519 ")
    assert len(one) > 60_000


# --- IMAGE_UPDATE on the agent side ---


def test_agent_reports_the_new_version_one_heartbeat_later(fleet, client):
    tmp, storage, _ = fleet
    seen: list[str] = []
    agent = _agent(client, fleet)
    real_post = client.post

    def spy(url, **kwargs):
        if url == "/v1/heartbeat":
            seen.append(Heartbeat.model_validate(kwargs["json"]).thermoctl.version)
        return real_post(url, **kwargs)

    client.post = spy  # type: ignore[method-assign]
    agent.tick()
    _apply(_ev(EventKind.IMAGE_UPDATE))
    expected = json.loads((tmp / "faults" / f"{APT}.version").read_text())["version"]
    agent.tick()
    assert storage.latest_desired_state_outcome(APT) is None
    agent.tick()
    agent.tick()
    assert seen == ["sim", "sim", expected, expected]
    outcome = storage.latest_desired_state_outcome(APT)
    assert outcome is not None and outcome.revision == 1 and outcome.successful is True
    assert storage.get_latest_heartbeat(APT).heartbeat.thermoctl.version == expected


def test_restarted_agent_adopts_the_installed_version_silently(fleet, client):
    tmp, storage, _ = fleet
    _apply(_ev(EventKind.IMAGE_UPDATE))
    expected = json.loads((tmp / "faults" / f"{APT}.version").read_text())["version"]
    _agent(client, fleet).tick()
    assert storage.get_latest_heartbeat(APT).heartbeat.thermoctl.version == expected
    assert storage.latest_desired_state_outcome(APT) is None


# --- TOKEN_EXPIRY / TENANT_CHANGE ---


def test_token_expiry_rotates_and_the_agent_keeps_heartbeating(fleet, client):
    tmp, storage, tokens = fleet
    path = tokens[APT]
    old = path.read_text().strip()
    inode = path.stat().st_ino
    agent = _agent(client, fleet)
    assert agent.tick() is True
    _apply(_ev(EventKind.TOKEN_EXPIRY))
    new = path.read_text().strip()
    assert new != old and new.startswith(f"agent_{APT}_")
    assert path.stat().st_ino == inode  # in place: a single-file bind mount keeps working
    assert (path.stat().st_mode & 0o777) == 0o600
    assert storage.get_apartment_token_hash(APT) is not None
    assert storage.apartment_reauth_pending(APT) is False
    stale = SimAgent(APT, client, old, healthy_sections)  # type: ignore[arg-type]
    assert stale.tick() is False
    assert agent.tick() is True
    assert agent.tick() is True


def test_token_expiry_leaves_history_alone(fleet, client):
    _, storage, _ = fleet
    _agent(client, fleet).tick()
    _apply(_ev(EventKind.TOKEN_EXPIRY))
    assert storage.get_latest_heartbeat(APT) is not None


def test_token_expiry_is_idempotent(fleet, client):
    _, _, tokens = fleet
    agent = _agent(client, fleet)
    _apply(_ev(EventKind.TOKEN_EXPIRY))
    _apply(_ev(EventKind.TOKEN_EXPIRY))
    assert agent.tick() is True


def test_token_expiry_completes_a_half_done_rotation(fleet, client):
    _, storage, tokens = fleet
    assert fleet_events.force_reauth(storage, APT) is True  # run died after step one
    assert fleet_events.force_reauth(storage, APT) is False
    agent = _agent(client, fleet)
    assert agent.tick() is False
    _apply(_ev(EventKind.TOKEN_EXPIRY))
    assert agent.tick() is True


def test_token_expiry_skips_vm_apartments(fleet):
    with pytest.raises(NotImplementedError, match="VM"):
        fleet_events.token_expiry(_ev(EventKind.TOKEN_EXPIRY, apt="sim-a01"))
    with pytest.raises(NotImplementedError, match="no container"):
        _apply(_ev(EventKind.TOKEN_EXPIRY, apt="sim-a01"))


def test_force_reauth_unknown_or_tokenless_apartment_is_false(fleet):
    _, storage, _ = fleet
    assert fleet_events.force_reauth(storage, "sim-a01") is False
    assert fleet_events.force_reauth(storage, "sim-a77") is False


def test_tenant_change_wipes_history_rotates_token_and_is_audited(fleet, client):
    _, storage, tokens = fleet
    other = _agent(client, fleet, "sim-a06")
    agent = _agent(client, fleet)
    assert agent.tick() and other.tick()
    old = tokens[APT].read_text().strip()
    _apply(_ev(EventKind.TENANT_CHANGE))
    assert storage.get_latest_heartbeat(APT) is None  # outgoing tenancy's history is gone
    assert storage.get_latest_heartbeat("sim-a06") is not None  # others untouched
    assert tokens[APT].read_text().strip() != old
    assert storage.apartment_reauth_pending(APT) is False
    assert agent.tick() is True
    assert storage.get_latest_heartbeat(APT) is not None
    assert other.tick() is True


def test_tenant_change_keeps_desired_state_and_backups(fleet, client):
    _, storage, _ = fleet
    agent = _agent(client, fleet)
    _apply(_ev(EventKind.IMAGE_UPDATE))
    _apply(_ev(EventKind.BACKUP, n="1"))
    agent.tick()
    _apply(_ev(EventKind.TENANT_CHANGE))
    assert storage.get_desired_state(APT) is not None
    assert len(storage.list_backups_for_apartment(APT)) == 1


def test_tenant_change_is_idempotent_and_survives_reapplication(fleet, client):
    agent = _agent(client, fleet)
    _apply(_ev(EventKind.TENANT_CHANGE))
    _apply(_ev(EventKind.TENANT_CHANGE))
    assert agent.tick() is True


def test_tenant_change_completes_a_rotation_left_pending(fleet, client):
    _, storage, tokens = fleet
    storage.rotate_apartment_token_for_tenant_change(APT, "half way", CONFIRM_USER, NOW)
    agent = _agent(client, fleet)
    assert agent.tick() is False
    _apply(_ev(EventKind.TENANT_CHANGE))
    assert agent.tick() is True


def test_tenant_change_refuses_a_locked_out_apartment(fleet):
    _, storage, _ = fleet
    storage.rotate_apartment_token_for_tenant_change(APT, "one", CONFIRM_USER, NOW)
    storage.rotate_apartment_token_for_tenant_change(APT, "two", CONFIRM_USER, NOW)  # drops it
    with pytest.raises(RuntimeError, match="start over"):
        _apply(_ev(EventKind.TENANT_CHANGE))


def test_tenant_change_on_a_vm_apartment_acts_fleet_side_only(fleet):
    tmp, storage, _ = fleet
    _apply(_ev(EventKind.TENANT_CHANGE, apt="sim-a01"))
    assert not (tmp / "agent-tokens" / "sim-a01.token").exists()
    assert storage.get_apartment("sim-a01") is not None


def test_tenant_change_deletes_diagnostic_bundle_blobs(fleet, monkeypatch):
    tmp, storage, _ = fleet
    blob = tmp / "diagnostic-bundles" / "x" / "bundle.age"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"opaque")
    real = type(storage).rotate_apartment_token_for_tenant_change

    def with_bundle(self, *args, **kwargs):
        real(self, *args, **kwargs)
        return TenantChangeOutcome(ok=True, diagnostic_bundle_storage_paths=["x/bundle.age"])

    monkeypatch.setattr(type(storage), "rotate_apartment_token_for_tenant_change", with_bundle)
    _apply(_ev(EventKind.TENANT_CHANGE))
    assert not blob.exists()


def test_tenant_change_unknown_apartment_in_database(fleet, monkeypatch):
    monkeypatch.setattr(type(fleet[1]), "get_apartment", lambda self, a: None)
    with pytest.raises(RuntimeError, match="not in the fleet database"):
        _apply(_ev(EventKind.TENANT_CHANGE))


def test_every_kind_has_a_fleet_side_or_container_handler():
    assert set(HANDLERS) == set(EventKind)

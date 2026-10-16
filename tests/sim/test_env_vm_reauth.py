"""VM token rotation: the fleet leaves 'pending', the device re-authenticates itself."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from fleet.storage import ApartmentRecord, create_storage, hash_token
from tools.sim.clock import clock_path
from tools.sim.env import apply_event_with, fleet_events, fleet_ops, vm_reauth
from tools.sim.env.enroll import enroll_container_apartments
from tools.sim.env.fleet_setup import seed_fleet
from tools.sim.scenario import Event, EventKind

NOW = datetime(2026, 2, 2, 8, tzinfo=UTC)
APT = "sim-a01"
DEVICE_TOKEN = "agent_sim-a01_device-held-token"  # noqa: S105 - test value


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    clock_path(tmp_path).parent.mkdir()
    clock_path(tmp_path).write_text("2026-02-02 08:00:00\n")
    (tmp_path / "fleet-data").mkdir()
    url = f"sqlite:///{tmp_path / 'fleet-data' / 'fleet.db'}"
    seed_fleet(tmp_path, url)
    enroll_container_apartments(tmp_path, url, NOW)
    storage = create_storage(url)
    with storage.session() as session:
        session.execute(
            update(ApartmentRecord)
            .where(ApartmentRecord.id == APT)
            .values(token_hash=hash_token(DEVICE_TOKEN))
        )
    monkeypatch.setattr(
        fleet_events,
        "run_op",
        lambda op, args: fleet_ops.run_op(storage, op, args, tmp_path / "bundles"),
    )
    return storage


class Vm:
    """Records the commands; answers the token read like `limactl shell ... cat`."""

    def __init__(self, token=DEVICE_TOKEN, fail=()):
        self.token, self.fail, self.cmds = token, fail, []

    def __call__(self, argv):
        self.cmds.append(argv)
        if any(word in argv for word in self.fail):
            raise subprocess.CalledProcessError(1, argv, stderr="boom")
        return SimpleNamespace(stdout=(self.token + "\n") if "cat" in argv else "")

    def verbs(self):
        return [c for c in self.cmds if "docker" in c]


def _ev(kind):
    return Event(f"{kind}:{APT}:0", 1, kind, APT)


def _reauth_target(storage):
    return storage.get_apartment_id_by_reauth_old_token_hash(hash_token(DEVICE_TOKEN))


def test_tenant_change_leaves_pending_and_never_completes_the_rotation(fleet):
    vm = Vm()
    apply_event_with(_ev(EventKind.TENANT_CHANGE), vm)
    assert fleet.apartment_reauth_pending(APT)
    assert _reauth_target(fleet) == APT  # the device's own token still gets 'reauth_required'
    assert fleet.get_apartment(APT) is not None


def test_sequence_stop_agent_reauth_in_one_shot_container_start_agent(fleet):
    vm = Vm()
    apply_event_with(_ev(EventKind.TENANT_CHANGE), vm)
    flat = [" ".join(c[5:]) if c[:2] == ["limactl", "shell"] else " ".join(c) for c in vm.cmds]
    assert all(c[:5] == ["limactl", "shell", "thermoctl-sim-a01", "--", "sudo"] for c in vm.cmds)
    assert flat[0].startswith("cat ")
    assert flat[1] == "docker stop thermoctl-agent"
    assert "compose" in flat[2] and " run --rm --no-deps -T agent python -c " in flat[2]
    assert flat[2].endswith(" sim-a01") and "reauthenticate(" in flat[2]
    assert flat[3] == "docker start thermoctl-agent"
    assert len(flat) == 4


def test_the_sim_sender_is_not_touched(fleet):
    vm = Vm()
    apply_event_with(_ev(EventKind.TOKEN_EXPIRY), vm)
    assert not any("thermoctl-sim-agent" in " ".join(c) for c in vm.cmds)


def test_token_expiry_forces_reauth_anchored_on_the_device_token(fleet):
    apply_event_with(_ev(EventKind.TOKEN_EXPIRY), Vm())
    assert fleet.apartment_reauth_pending(APT) and _reauth_target(fleet) == APT


def test_a_device_the_fleet_already_replaced_is_repaired(fleet):
    """The state run 2 left sim-a01 in: fleet knows only a new token, device the old one."""
    with fleet.session() as session:
        session.execute(
            update(ApartmentRecord)
            .where(ApartmentRecord.id == APT)
            .values(token_hash=hash_token("agent_sim-a01_new-token-nobody-has"))
        )
    assert not fleet.apartment_reauth_pending(APT)
    vm = Vm()
    vm_reauth.repair(APT, vm)
    assert fleet.apartment_reauth_pending(APT) and _reauth_target(fleet) == APT


def test_repair_is_idempotent_and_keeps_history(fleet):
    vm_reauth.repair(APT, Vm())
    vm_reauth.repair(APT, Vm())
    assert _reauth_target(fleet) == APT


def test_a_repeated_tenant_change_does_not_wipe_twice_or_lock_out(fleet):
    apply_event_with(_ev(EventKind.TENANT_CHANGE), Vm())
    apply_event_with(_ev(EventKind.TENANT_CHANGE), Vm())
    assert _reauth_target(fleet) == APT


def test_a_vm_that_cannot_be_read_changes_nothing_on_the_fleet(fleet):
    with pytest.raises(RuntimeError, match="no agent token"):
        apply_event_with(_ev(EventKind.TENANT_CHANGE), Vm(token=""))
    assert not fleet.apartment_reauth_pending(APT)
    with pytest.raises(subprocess.CalledProcessError):
        apply_event_with(_ev(EventKind.TENANT_CHANGE), Vm(fail=("cat",)))
    assert not fleet.apartment_reauth_pending(APT)


def test_a_missing_agent_container_does_not_stop_the_recovery(fleet):
    vm = Vm(fail=("stop",))
    apply_event_with(_ev(EventKind.TOKEN_EXPIRY), vm)
    assert vm.cmds[-1][-2:] == ["start", "thermoctl-agent"]


def test_a_failed_device_reauthentication_leaves_the_agent_stopped(fleet):
    vm = Vm(fail=("compose",))
    with pytest.raises(subprocess.CalledProcessError):
        apply_event_with(_ev(EventKind.TOKEN_EXPIRY), vm)
    assert not any(c[-2:] == ["start", "thermoctl-agent"] for c in vm.cmds)
    assert fleet.apartment_reauth_pending(APT)  # a re-run re-anchors, nothing is lost


def test_only_vm_apartments(fleet):
    with pytest.raises(ValueError, match="not a VM apartment"):
        vm_reauth.reauth_vm("sim-a05", "repair", "x", Vm())


def test_ops_validate_mode_and_pending(fleet):
    with pytest.raises(ValueError, match="mode"):
        fleet_ops.begin_device_reauth(fleet, APT, "bogus", "e", None, NOW, None)
    with pytest.raises(RuntimeError, match="no rotation pending"):
        fleet_ops.begin_device_reauth(fleet, "sim-a02", "repair", "e", None, NOW, None)


def test_manual_repair_main(fleet, monkeypatch, capsys):
    vm = Vm()
    monkeypatch.setattr(vm_reauth, "_run", vm)
    assert vm_reauth.main([APT]) == 0
    assert vm_reauth.main([]) == 2
    assert vm_reauth.main(["sim-a05"]) == 1
    assert "not a VM apartment" in capsys.readouterr().out


def test_device_script_hashes_match_the_fleet_hash():
    assert hashlib.sha256(b"x").hexdigest() == hash_token("x")
    compile(vm_reauth.DEVICE_SCRIPT, "device", "exec")

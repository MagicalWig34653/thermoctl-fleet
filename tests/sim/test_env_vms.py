"""The VM base stations: argv building, idempotence, exact-name deletion, the enrolment flow
(with a fake runner and a fake fleet; the live run is documented in docs/sim-findings/02)."""

import json
import stat
import subprocess
from pathlib import Path

import pytest

from protocol.registration import AgentRegistrationFile
from tools.sim.env import fleet_events, vms
from tools.sim.env.config import VM_SIM_CONTAINER
from tools.sim.env.tls import ensure_tls

FP = "sha256:" + "ab" * 32
LIB = "/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1"


class FakeRun:
    """Records argv lists; answers by substring of the joined argv (first match wins)."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, *, timeout=120.0, check=True):  # noqa: ANN001, ANN201
        self.calls.append(argv)
        text = " ".join(argv)
        for needle, (code, out) in self.answers.items():
            if needle in text:
                if check and code:
                    raise subprocess.CalledProcessError(code, argv, output=out, stderr="boom")
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def _list(**states: str) -> str:
    return "\n".join(json.dumps({"name": n, "status": s}) for n, s in states.items()) + "\n"


def test_parse_instances():
    assert vms.parse_instances(_list(a="Running", b="Stopped") + "\n") == {
        "a": "Running",
        "b": "Stopped",
    }
    assert vms.parse_instances("") == {}


def test_create_argv_uses_the_mac_test_vm_template_and_read_only_mounts(tmp_path):
    argv = vms.create_argv("sim-a02", tmp_path, repo=Path("/some/checkout"))
    assert argv[:4] == ["limactl", "create", "--name", "thermoctl-sim-a02"]
    assert "--tty=false" in argv
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--memory") + 1] == "3"
    assert argv[argv.index("--disk") + 1] == "8"
    assert argv[-1].endswith("tools/mac-test-vm.lima.yaml")
    sets = [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]
    assert sets[0] == '.mounts[0].location = "/some/checkout"'
    assert f'"location": "{tmp_path / "clock"}"' in sets[1]
    assert '"mountPoint": "/sim-clock", "writable": false' in sets[1]
    assert '"mountPoint": "/sim-faults", "writable": false' in sets[1]
    assert 'writable": true' not in sets[1]


def test_create_argv_rejects_foreign_apartments_and_odd_paths(tmp_path):
    with pytest.raises(ValueError):
        vms.create_argv("sim-a03", tmp_path)
    with pytest.raises(ValueError, match="unsupported character"):
        vms.create_argv("sim-a01", tmp_path / 'we"ird')


def test_shell_argv_is_an_argv_list_for_the_exact_vm():
    assert vms.sudo_argv("sim-a01", "docker", "ps") == [
        "limactl", "shell", "thermoctl-sim-a01", "--", "sudo", "docker", "ps",
    ]  # fmt: skip
    with pytest.raises(ValueError):
        vms.shell_argv("thermoctl-test-vm", "true")


def test_register_argv_runs_the_real_register_detached():
    argv = vms.register_argv("sim-a01", LIB)
    text = " ".join(argv)
    assert argv[-4:] == ["python", "-m", "agent", "register"]
    assert "thermoctl-agent:local" in argv and "-d" in argv
    assert f"{LIB}:/usr/local/lib/libfaketime.so.1:ro" in text
    assert "FAKETIME_TIMESTAMP_FILE=/sim-clock/clock" in text


def test_stack_env_validates_its_inputs():
    text = vms.stack_env("sim-a01", FP, LIB)
    assert "SIM_FLEET_URL=https://host.lima.internal:8243\n" in text
    assert f"SIM_FINGERPRINT={FP}\n" in text
    for bad_fp in ("sha256:xyz", "abc", FP + "\nX=1"):
        with pytest.raises(ValueError):
            vms.stack_env("sim-a01", bad_fp, LIB)
    with pytest.raises(ValueError):
        vms.stack_env("sim-a01", FP, "/tmp/evil; rm -rf /")  # noqa: S108
    with pytest.raises(ValueError):
        vms.stack_env("sim-a09", FP, LIB)


def test_listen_host_defaults_to_loopback_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("SIM_TLS_LISTEN_HOST", raising=False)
    assert vms.listen_host() == "127.0.0.1"
    monkeypatch.setenv("SIM_TLS_LISTEN_HOST", "192.168.5.2")
    assert vms.listen_host() == "192.168.5.2"


def _seed_registration(tmp_path: Path) -> None:
    (tmp_path / "registrations").mkdir()
    AgentRegistrationFile(
        fleet_address="http://127.0.0.1:8200",
        certificate_fingerprint="none-sim-plain-http",
        registration_code="CODE-FROM-SEED-1234",
    )
    for apartment in ("sim-a01", "sim-a02"):
        (tmp_path / "registrations" / f"{apartment}.json").write_text(
            json.dumps(
                {
                    "fleet_address": "http://127.0.0.1:8200",
                    "certificate_fingerprint": "none-sim-plain-http",
                    "registration_code": "CODE-FROM-SEED-1234",
                }
            )
        )


def test_vm_registration_readdresses_the_seeded_code_to_the_tls_front(tmp_path):
    _seed_registration(tmp_path)
    material = ensure_tls(tmp_path)
    target = vms.write_vm_registration(tmp_path, "sim-a01", material)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    written = AgentRegistrationFile.model_validate_json(target.read_text())
    assert written.fleet_address == "https://host.lima.internal:8243"
    assert written.certificate_fingerprint == material.fingerprint
    assert written.registration_code == "CODE-FROM-SEED-1234"
    seeded = json.loads((tmp_path / "registrations" / "sim-a01.json").read_text())
    assert seeded["fleet_address"] == "http://127.0.0.1:8200"  # the original is untouched
    assert vms.write_vm_registration(tmp_path, "sim-a01", material) == target  # re-runnable


def test_wait_until_polls_and_times_out():
    sleeps = []
    answers = iter([False, False, True])
    vms.wait_until("x", lambda: next(answers), timeout_s=30, sleep=sleeps.append, interval_s=3)
    assert sleeps == [3, 3]
    with pytest.raises(RuntimeError, match="timed out"):
        vms.wait_until("y", lambda: False, timeout_s=6, sleep=lambda s: None, interval_s=3)


def test_down_deletes_exactly_the_two_sim_vms_and_nothing_else(tmp_path):
    run = FakeRun(
        {
            "list --json": (
                0,
                _list(
                    **{
                        "thermoctl-test-vm": "Stopped",
                        "thermoctl-e2e-basestation": "Stopped",
                        "thermoctl-sim-a01": "Running",
                        "thermoctl-sim-a01-old": "Stopped",
                        "thermoctl-sim-a02": "Stopped",
                        "thermoctl-sim-a03": "Stopped",
                    }
                ),
            )
        }
    )
    for marked in ("thermoctl-sim-a01", "thermoctl-sim-a02", "thermoctl-sim-a03"):
        vms.record_created(tmp_path, marked)
    stopped = []
    vms.down(tmp_path, run, stop_proxy=lambda d: stopped.append(d) or True, log=lambda m: None)
    deletes = [c for c in run.calls if c[:2] == ["limactl", "delete"]]
    assert deletes == [
        ["limactl", "delete", "--force", "thermoctl-sim-a01"],
        ["limactl", "delete", "--force", "thermoctl-sim-a02"],
    ]
    assert stopped == [tmp_path]


def test_down_never_deletes_a_vm_that_is_not_in_the_marker(tmp_path):
    run = FakeRun(
        {
            "list --json": (
                0,
                _list(**{"thermoctl-sim-a01": "Running", "thermoctl-sim-a02": "Stopped"}),
            )
        }
    )
    vms.record_created(tmp_path, "thermoctl-sim-a02")
    messages = []
    vms.down(tmp_path, run, stop_proxy=lambda d: False, log=messages.append)
    deletes = [c for c in run.calls if c[:2] == ["limactl", "delete"]]
    assert deletes == [["limactl", "delete", "--force", "thermoctl-sim-a02"]]
    assert any("a01" in m and "NOT deleting" in m for m in messages)
    assert vms.read_created(tmp_path) == {}  # a02 forgotten, a01 never listed


def test_down_without_any_marker_deletes_nothing(tmp_path):
    run = FakeRun({"list --json": (0, _list(**{"thermoctl-sim-a01": "Running"}))})
    vms.down(tmp_path, run, stop_proxy=lambda d: False, log=lambda m: None)
    assert not [c for c in run.calls if "delete" in c]


def test_marker_is_private_and_roundtrips(tmp_path):
    assert vms.read_created(tmp_path) == {}
    vms.record_created(tmp_path, "thermoctl-sim-a01")
    marker = tmp_path / "vms-created.json"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    data = vms.read_created(tmp_path)
    assert list(data) == ["thermoctl-sim-a01"] and data["thermoctl-sim-a01"].startswith("20")
    vms.forget_created(tmp_path, "thermoctl-sim-a01")
    assert not marker.exists()


def test_corrupt_marker_is_an_error_not_an_empty_list(tmp_path):
    (tmp_path / "vms-created.json").write_text("[1, 2]")
    with pytest.raises(RuntimeError, match="corrupt"):
        vms.read_created(tmp_path)
    (tmp_path / "vms-created.json").write_text("{nope")
    with pytest.raises(RuntimeError, match="corrupt"):
        vms.read_created(tmp_path)


def test_ensure_vm_refuses_a_vm_the_sim_did_not_create(tmp_path):
    directory = _prepared(tmp_path)
    run = FakeRun({"list --json": (0, _list(**{"thermoctl-sim-a01": "Running"}))})
    with pytest.raises(RuntimeError, match="thermoctl-sim-a01.*not created by the sim"):
        vms.up(directory, run, ["sim-a01"], start_proxy=lambda *a: 1, log=lambda m: None)
    assert run.calls == [["limactl", "list", "--json"]]  # nothing touched


def test_down_with_nothing_to_delete_deletes_nothing(tmp_path):
    run = FakeRun({"list --json": (0, _list(**{"thermoctl-test-vm": "Running"}))})
    vms.down(tmp_path, run, stop_proxy=lambda d: False, log=lambda m: None)
    assert not [c for c in run.calls if "delete" in c]


def test_down_rejects_a_non_vm_apartment(tmp_path):
    with pytest.raises(ValueError):
        vms.down(
            tmp_path, FakeRun({"list --json": (0, "")}), ["sim-a03"], stop_proxy=lambda d: False
        )


def _prepared(tmp_path: Path) -> Path:
    for sub in ("clock", "faults"):
        (tmp_path / sub).mkdir()
    _seed_registration(tmp_path)
    return tmp_path


def test_up_requires_the_fleet_state(tmp_path):
    with pytest.raises(RuntimeError, match="run up_fleet first"):
        vms.up(tmp_path, FakeRun(), start_proxy=lambda *a: 1)


def test_up_is_idempotent_for_a_running_enrolled_vm(tmp_path):
    directory = _prepared(tmp_path)
    run = FakeRun(
        {
            "list --json": (0, _list(**{"thermoctl-sim-a01": "Running"})),
            "find /usr/lib": (0, LIB + "\n"),
        }
    )
    vms.record_created(directory, "thermoctl-sim-a01")
    messages = []
    vms.up(directory, run, ["sim-a01"], start_proxy=lambda *a: 99, log=messages.append)
    joined = run.joined()
    assert not any(c.startswith("limactl create") for c in joined)
    assert not any(c.startswith("limactl start") for c in joined)
    assert not any("agent register" in c for c in joined)
    assert any("systemctl enable --now thermoctl-sim-stack.service" in c for c in joined)
    assert any("already enrolled" in m for m in messages)
    assert (directory / "vm" / "sim-a01" / "stack.env").read_text().count("\n") == 4


def test_up_creates_and_starts_a_missing_vm_then_enrols_it(tmp_path, monkeypatch):
    directory = _prepared(tmp_path)
    run = FakeRun(
        {
            "list --json": (0, _list(**{"thermoctl-test-vm": "Stopped"})),
            "find /usr/lib": (0, LIB + "\n"),
            "test -s /var/lib/thermoctl-agent/agent_token": (1, ""),
        }
    )
    seen = []
    monkeypatch.setattr(vms, "enroll", lambda *a, **k: seen.append(a[2]) or "123456")
    vms.up(directory, run, ["sim-a02"], start_proxy=lambda *a: 99, log=lambda m: None)
    joined = run.joined()
    assert "thermoctl-sim-a02" in vms.read_created(directory)
    assert joined[1].startswith("limactl create --name thermoctl-sim-a02")
    assert "limactl start --tty=false thermoctl-sim-a02" in joined
    assert seen == ["sim-a02"]
    # the test VM of the user is never named in any command
    assert not any("thermoctl-test-vm" in c for c in joined[2:])


class FakeFleet:
    def __init__(self, code="654321"):
        self.code = code
        self.ops = []

    def __call__(self, op, args):
        self.ops.append((op, args))
        if op == "device_report":
            return {"state": "reported", "verification_code": self.code}
        return {"state": "in_service"}


def _enroll_run(printed: str):
    return FakeRun(
        {
            "docker logs thermoctl-agent-register": (0, f"Verification code: {printed}\n"),
            "docker inspect -f": (0, "exited 0\n"),
            "test -s /var/lib/thermoctl-agent/agent_token": (0, ""),
        }
    )


def test_enroll_confirms_with_the_code_from_the_fleet_database(tmp_path, monkeypatch):
    fleet = FakeFleet()
    monkeypatch.setattr(fleet_events, "run_op", fleet)
    run = _enroll_run("654321")
    code = vms.enroll(run, tmp_path, "sim-a01", LIB, sleep=lambda s: None)
    assert code == "654321"
    confirm = [a for op, a in fleet.ops if op == "confirm_device"]
    assert confirm and confirm[0]["verification_code"] == "654321"
    assert confirm[0]["device_id"] == "sim-d01" and confirm[0]["apartment"] == "sim-a01"
    joined = run.joined()
    assert any("agent register" in c for c in joined)  # the REAL registration, detached
    assert joined[-1].endswith("docker rm -f thermoctl-agent-register")


def test_enroll_refuses_to_confirm_when_the_codes_differ(tmp_path, monkeypatch):
    fleet = FakeFleet()
    monkeypatch.setattr(fleet_events, "run_op", fleet)
    with pytest.raises(RuntimeError, match="does not match"):
        vms.enroll(_enroll_run("111111"), tmp_path, "sim-a01", LIB, sleep=lambda s: None)
    assert not [op for op, _ in fleet.ops if op == "confirm_device"]


def test_enroll_fails_if_no_token_was_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_events, "run_op", FakeFleet())
    run = _enroll_run("654321")
    run.answers = {**run.answers, "test -s /var/lib/thermoctl-agent/agent_token": (1, "")}
    with pytest.raises(RuntimeError, match="no agent token"):
        vms.enroll(run, tmp_path, "sim-a01", LIB, sleep=lambda s: None)


def test_status_lists_each_vm(tmp_path):
    run = FakeRun(
        {
            "list --json": (0, _list(**{"thermoctl-sim-a01": "Running"})),
            "docker ps": (0, "thermoctl-agent\nthermoctl-sim-agent\n"),
        }
    )
    text = vms.status(run)
    assert (
        "thermoctl-sim-a01: Running, enrolled, containers: thermoctl-agent thermoctl-sim-agent"
        in text
    )
    assert "thermoctl-sim-a02: absent" in text


def test_logs_collects_agent_sender_and_watchdog(tmp_path):
    run = FakeRun({"docker logs --tail 5 thermoctl-agent": (0, "agent line\n")})
    text = vms.logs(run, "sim-a01", 5)
    assert "== thermoctl-agent ==" in text and "agent line" in text
    assert "== thermoctl-sim-agent ==" in text and "thermoctl-watchdog" in text


def test_provision_base_builds_the_image_only_when_missing():
    have = FakeRun()  # everything "present" (returncode 0)
    vms.provision_base(have, "sim-a01")
    assert not any("docker build" in c or "apt-get" in c for c in have.joined())
    missing = FakeRun(
        {"dpkg -s faketime": (1, ""), "docker image inspect": (1, "")},
    )
    vms.provision_base(missing, "sim-a01")
    joined = missing.joined()
    assert any("apt-get install -y --no-install-recommends faketime" in c for c in joined)
    assert any(
        "docker build -f docker/Dockerfile.agent -t thermoctl-agent:local" in c for c in joined
    )


def test_find_faketime_lib_errors_when_missing():
    with pytest.raises(RuntimeError, match="not found"):
        vms.find_faketime_lib(FakeRun({"find /usr/lib": (0, "\n")}), "sim-a01")


def test_vm_assets_match_what_sync_assets_installs_and_stay_read_only():
    vm_dir = Path(vms.__file__).parent / "vm"
    compose = (vm_dir / "compose.vm.yml").read_text()
    unit = (vm_dir / vms.STACK_UNIT).read_text()
    assert f"{vms.VM_DIR}/compose.yml" in unit and f"{vms.VM_DIR}/stack.env" in unit
    assert "mountpoint -q /sim-clock" in unit and "mountpoint -q /sim-faults" in unit
    assert "container_name: thermoctl-agent\n" in compose
    assert f"container_name: {VM_SIM_CONTAINER}\n" in compose
    # the clock, faults, token directory and tools are mounted read-only
    for mount in (
        "/sim-clock:/sim-clock:ro",
        "/sim-faults:/sim-state/faults:ro",
        "/var/lib/thermoctl-agent:/agent-data:ro",
        "/opt/thermoctl-sim/tools:/app/tools:ro",
    ):
        assert mount in compose
    assert "ports:" not in compose and "network_mode" not in compose

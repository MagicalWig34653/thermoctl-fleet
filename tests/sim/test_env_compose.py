"""The generated/hand-kept compose file: agents are isolated from every secret but their own."""

import json
import os
import shutil
import subprocess

import pytest

from tools.sim.env import up_fleet
from tools.sim.env.config import NETWORK, PREFIX
from tools.sim.env.enroll import CONTAINER_APARTMENTS
from tools.sim.env.events import container_name

DUMMY = {
    "SIM_STATE_DIR": "/dummy/state",
    "SIM_UID": "1000",
    "SIM_GID": "1000",
    "SIM_TOTP_KEY": "dummy",
}
FORBIDDEN = ("users.json", "totp.key", "registrations", "docker.sock", "fleet-data", "backups")


def _yaml_services():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(up_fleet.COMPOSE_FILE.read_text())["services"]


def _rendered_services():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    cmd = ["docker", "compose", "-p", PREFIX, "-f", str(up_fleet.COMPOSE_FILE)]
    done = subprocess.run(  # noqa: S603
        [*cmd, "config", "--format", "json"],
        env={**os.environ, **DUMMY},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {done.stderr[:100]}")
    return json.loads(done.stdout)["services"]


@pytest.fixture(params=["yaml", "docker"])
def services(request):
    return _yaml_services() if request.param == "yaml" else _rendered_services()


def _agents(services):
    return {n: s for n, s in services.items() if n != "fleet"}


def _mount_strings(service):
    out = []
    for vol in service.get("volumes", []):
        out.append(
            vol
            if isinstance(vol, str)
            else f"{vol['source']}:{vol['target']}:ro={vol.get('read_only')}"
        )
    return out


def test_ten_agent_services_with_handler_compatible_container_names(services):
    agents = _agents(services)
    assert sorted(agents) == sorted(f"agent-{a}" for a in CONTAINER_APARTMENTS)
    assert len(agents) == 10
    for apartment in CONTAINER_APARTMENTS:
        assert agents[f"agent-{apartment}"]["container_name"] == container_name(apartment)
    assert "sim-a01" not in " ".join(agents) and "sim-a02" not in " ".join(agents)


def test_only_the_fleet_publishes_ports(services):
    assert services["fleet"]["ports"]
    for name, service in _agents(services).items():
        assert not service.get("ports"), name
        assert not service.get("expose"), name


def test_agents_run_their_own_apartment_against_the_fleet_service(services):
    for apartment in CONTAINER_APARTMENTS:
        cmd = services[f"agent-{apartment}"]["command"]
        assert cmd[:3] == ["python", "-m", "tools.sim.sim_agent"]
        assert cmd[cmd.index("--apartment") + 1] == apartment
        assert cmd[cmd.index("--fleet") + 1] == "http://fleet:8000"
        assert cmd[cmd.index("--token-file") + 1] == "/run/secrets/agent.token"
        assert cmd[cmd.index("--interval-s") + 1] == "5"


def test_agents_mount_only_their_own_token_and_nothing_secret(services):
    for apartment in CONTAINER_APARTMENTS:
        mounts = _mount_strings(services[f"agent-{apartment}"])
        text = "\n".join(mounts)
        assert len(mounts) == 4, mounts
        assert f"agent-tokens/{apartment}.token" in text
        assert text.count("agent-tokens") == 1  # no other apartment's token, no token directory
        for other in CONTAINER_APARTMENTS:
            if other != apartment:
                assert other not in text
        for bad in FORBIDDEN:
            assert bad not in text, bad


def test_agent_mounts_are_all_read_only(services):
    for name, service in _agents(services).items():
        for vol in service["volumes"]:
            if isinstance(vol, str):
                assert vol.endswith(":ro"), (name, vol)
            else:
                assert vol["read_only"] is True, (name, vol)


def test_agents_are_unprivileged_and_confined_to_the_sim_network(services):
    for name, service in _agents(services).items():
        assert not service.get("privileged"), name
        assert not service.get("cap_add"), name
        assert service.get("network_mode") is None, name
        assert list(service["networks"]) == ["sim-net"], name
        assert service["restart"] == "unless-stopped"
        assert service["image"] == "thermoctl-sim-fleet:local"
        assert service["environment"]["FAKETIME_TIMESTAMP_FILE"] == "/sim-clock/clock"
        assert service["environment"]["SIM_ALLOWED_HOSTS"] == "fleet"
        assert "pid" not in service and "ipc" not in service


def test_network_name_matches_the_event_handlers(services):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(up_fleet.COMPOSE_FILE.read_text())
    assert doc["networks"]["sim-net"]["name"] == NETWORK


def test_agents_do_not_inherit_the_fleet_healthcheck(services):
    for name, svc in services.items():
        if name.startswith("agent-"):
            assert svc["healthcheck"] == {"disable": True}, name

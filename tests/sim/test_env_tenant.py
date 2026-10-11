"""Tenant instances: pure helpers, compose file shape and the CLI wiring (no Docker)."""

import stat
from pathlib import Path

import pytest

from tools.sim.env import tenant
from tools.sim.env.events import tenant_container_name


def test_container_name_validates_and_prefixes():
    assert tenant_container_name(1) == "thermoctl-sim-tenant1"
    assert tenant_container_name(3) == "thermoctl-sim-tenant3"
    for bad in (0, 4, -1):
        with pytest.raises(ValueError):
            tenant_container_name(bad)
    with pytest.raises(ValueError):
        tenant_container_name("1; rm")  # type: ignore[arg-type]


def test_ports_urls_and_roles():
    assert [tenant.tenant_port(n) for n in (1, 2, 3)] == [8301, 8302, 8303]
    assert tenant.tenant_urls() == {
        "tenant1": "http://127.0.0.1:8301",
        "tenant2": "http://127.0.0.1:8302",
        "tenant3": "http://127.0.0.1:8303",
    }
    # `logins.role_of` strips "sim-": the username is what names the storage state.
    from tools.sim.env.logins import role_of

    assert [role_of(tenant.tenant_username(n)) for n in (1, 2, 3)] == [
        "tenant1",
        "tenant2",
        "tenant3",
    ]


def test_setup_token_is_scraped_from_the_log():
    log = "INFO x\nEinrichtung: Einmal-Token (gültig 60 Minuten): AbC-123_xyz\nINFO y\n"
    assert tenant.extract_setup_token(log) == "AbC-123_xyz"
    assert tenant.extract_setup_token("no token here") is None


def test_new_users_have_distinct_long_secrets():
    users = tenant.new_users()
    assert set(users) == {
        "sim-tenant1",
        "sim-tenant2",
        "sim-tenant3",
        "sim-tenant1-admin",
        "sim-tenant2-admin",
        "sim-tenant3-admin",
    }
    passwords = [u["password"] for u in users.values()]
    assert len(set(passwords)) == len(passwords)
    assert all(len(p) >= 20 for p in passwords)  # sibling minimum is 12


def test_users_file_is_private_from_creation(tmp_path):
    tenant.write_users(tmp_path, {"sim-tenant1": {"password": "x" * 20}})
    path = tmp_path / "tenant-users.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert tenant.load_users(tmp_path) == {"sim-tenant1": {"password": "x" * 20}}
    assert tenant.load_users(tmp_path / "missing") is None


def test_secret_key_is_created_once_and_private(tmp_path):
    first = tenant.secret_key(tmp_path)
    assert len(first) >= 32
    assert tenant.secret_key(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "tenant-secret.key").stat().st_mode) == 0o600


def test_compose_env_names_state_and_source(tmp_path):
    env = tenant.compose_env(tmp_path, "k" * 40, {"PATH": "/bin"}, source=Path("/sib"))
    assert env["SIM_STATE_DIR"] == str(tmp_path)
    assert env["SIM_TENANT_SECRET_KEY"] == "k" * 40
    assert env["SIM_THERMOCTL_SRC"] == "/sib" and env["PATH"] == "/bin"


def test_up_builds_base_then_composes_then_bootstraps(tmp_path):
    calls = []
    boot = []
    (tmp_path / "sib" / "docker").mkdir(parents=True)
    (tmp_path / "sib" / "docker" / "Dockerfile").write_text("FROM scratch\n")
    tenant.up(
        tmp_path,
        lambda cmd, env: calls.append(cmd),
        source=tmp_path / "sib",
        wait=lambda n: None,
        bootstrap=lambda d, ns: boot.append((d, ns)),
    )
    assert calls[0][:2] == ["docker", "build"] and "thermoctl-sim-tenant-base:local" in calls[0]
    assert calls[0][-1] == str(tmp_path / "sib")
    assert calls[1][:6] == [
        "docker",
        "compose",
        "-p",
        "thermoctl-sim-tenants",
        "-f",
        str(tenant.COMPOSE_FILE),
    ]
    assert calls[1][-3:] == ["up", "-d", "--build"]
    assert boot == [(tmp_path, (1, 2, 3))]
    # the clock exists before any container reads it
    assert (tmp_path / "clock" / "clock").is_file()


def test_down_removes_only_the_tenant_project_and_its_volumes(tmp_path):
    calls = []
    tenant.down(tmp_path, lambda cmd, env: calls.append(cmd))
    assert calls == [
        [
            "docker",
            "compose",
            "-p",
            "thermoctl-sim-tenants",
            "-f",
            str(tenant.COMPOSE_FILE),
            "down",
            "--volumes",
            "--remove-orphans",
        ]
    ]


def test_up_refuses_a_missing_sibling_repository(tmp_path):
    with pytest.raises(RuntimeError, match="sibling thermoctl repository"):
        tenant.up(tmp_path, lambda c, e: None, source=tmp_path / "nope")


def test_login_success_is_leaving_the_login_page():
    base = "http://127.0.0.1:8301"
    assert tenant.login_succeeded(f"{base}/", base)
    assert not tenant.login_succeeded(f"{base}/login", base)
    assert not tenant.login_succeeded("http://elsewhere/", base)


def test_seed_payload_never_reaches_argv():
    cmd = tenant.seed_command(2)
    assert cmd[:5] == ["docker", "exec", "-i", "-u", "thermoctl"]
    assert "thermoctl-sim-tenant2" in cmd
    assert not any("password" in part.lower() for part in cmd)


def test_main_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(tenant, "up", lambda d, run, **k: seen.append("up"))
    monkeypatch.setattr(tenant, "down", lambda d, run: seen.append("down"))
    monkeypatch.setattr(
        tenant, "refresh_logins", lambda d: seen.append("logins") or {"tenant1": True}
    )
    assert tenant.main(["--up"]) == 0
    assert tenant.main(["--down"]) == 0
    assert tenant.main(["--logins"]) == 0
    assert seen == ["up", "logins", "down", "logins"]  # --up also logs the tenants in
    monkeypatch.setattr(tenant, "refresh_logins", lambda d: {"tenant1": False})
    assert tenant.main(["--logins"]) == 1


def test_compose_file_shape():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(tenant.COMPOSE_FILE.read_text())
    assert doc["name"] == "thermoctl-sim-tenants"
    services = doc["services"]
    assert sorted(services) == ["tenant1", "tenant2", "tenant3"]
    for n in (1, 2, 3):
        svc = services[f"tenant{n}"]
        assert svc["container_name"] == tenant_container_name(n)
        assert svc["ports"] == [f"127.0.0.1:{8300 + n}:8000"]
        assert not svc.get("privileged")
        mounts = " ".join(svc["volumes"])
        assert "docker.sock" not in mounts
        assert ":/sim-clock:ro" in mounts and "/seed/tests:ro" in mounts
        assert svc["environment"]["FAKETIME_TIMESTAMP_FILE"] == "/sim-clock/clock"
        assert svc["environment"]["THERMOCTL_MQTT_ENABLED"] == "false"
    assert all(v["name"].startswith("thermoctl-sim") for v in doc["volumes"].values())

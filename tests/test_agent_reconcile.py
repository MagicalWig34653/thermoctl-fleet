"""Tests for `agent.loop.reconcile_desired_state` and its own helpers
(P5.4, docs/specification.md section 13) -- a real Docker Engine API double
over a real Unix socket (`tests/docker_api_support.py`, extended for this
package), never a stubbed reader, for every Docker-touching path.

**Stays inactive by owner decision (2026-09-28)**: the two fail-closed
gates (`pilot_mode`, and "unknown" thermoctl health/outdoor temperature)
are exercised first below, since they are -- per that decision -- the only
code path genuinely live in production right now. Every other test here
exercises the reconciliation logic itself, built and tested now so it is
ready the day both conditions are lifted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import time as dt_time
from pathlib import Path

import httpx
import pytest
from pyrage import x25519

from agent.loop import (
    BackupConfig,
    PendingSwap,
    ReconcileOutcome,
    _await_or_rollback_pending_swap,
    _load_pending_swap,
    _reconcile_precheck,
    _save_pending_swap,
    _select_service_to_update,
    container_is_healthy,
    current_repo_digest,
    image_repo_digests,
    pull_image_by_digest,
    reconcile_desired_state,
    verify_pulled_digest,
)
from agent.sources import ALLOWED_SOURCES
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from tests.docker_api_support import run_fake_docker_api_with_app

REPO_THERMOCTL = ALLOWED_SOURCES["thermoctl"]
REPO_ZIGBEE = ALLOWED_SOURCES["zigbee2mqtt"]
REPO_MOSQUITTO = ALLOWED_SOURCES["mosquitto"]
REPO_AGENT = ALLOWED_SOURCES["agent"]

OLD_THERMOCTL = "sha256:" + "a" * 64
NEW_THERMOCTL = "sha256:" + "b" * 64
OLD_ZIGBEE = "sha256:" + "c" * 64
NEW_ZIGBEE = "sha256:" + "d" * 64
OLD_MOSQUITTO = "sha256:" + "e" * 64
OLD_AGENT = "sha256:" + "f" * 64
NEW_AGENT = "sha256:" + "1" * 64

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _write_recipients_file(path: Path) -> None:
    identity_one = x25519.Identity.generate()
    identity_two = x25519.Identity.generate()
    path.write_text(
        f"{identity_one.to_public()}\n{identity_two.to_public()}\n", encoding="utf-8"
    )


def _mock_backup_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        content_hash = request.url.params.get("content_hash", "0" * 64)
        kind = request.url.params.get("kind", "operational_data")
        return httpx.Response(
            201,
            json={
                "id": "backup-1",
                "kind": kind,
                "received_at": NOW.isoformat(),
                "size_bytes": 1,
                "content_hash": content_hash,
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fleet.example")


def _backup_config(tmp_path: Path) -> BackupConfig:
    staging_dir = tmp_path / "staging"
    db_path = tmp_path / "thermoctl.db"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE t (x TEXT)")
    connection.commit()
    connection.close()
    z2m_dir = tmp_path / "zigbee2mqtt"
    z2m_dir.mkdir()
    recipients_file = tmp_path / "recipients.txt"
    _write_recipients_file(recipients_file)
    return BackupConfig(
        apartment_id="apt-7",
        agent_version="0.1.0-dev",
        staging_dir=staging_dir,
        thermoctl_db_path=db_path,
        zigbee2mqtt_dir=z2m_dir,
        client=_mock_backup_client(),
        recipients_file=recipients_file,
    )


def _desired_state(
    *,
    thermoctl_digest: str = OLD_THERMOCTL,
    zigbee_digest: str = OLD_ZIGBEE,
    mosquitto_digest: str = OLD_MOSQUITTO,
    agent_digest: str = OLD_AGENT,
    not_below_outdoor_temp_c: float = -5.0,
    window_from: dt_time = dt_time(0, 0),
    window_until: dt_time = dt_time(23, 59),
) -> DesiredState:
    return DesiredState(
        revision=1,
        services=Services(
            thermoctl=ServiceState(image=REPO_THERMOCTL, version="1.0", digest=thermoctl_digest),
            zigbee2mqtt=ServiceState(image=REPO_ZIGBEE, version="1.0", digest=zigbee_digest),
            mosquitto=ServiceState(image=REPO_MOSQUITTO, version="1.0", digest=mosquitto_digest),
            agent=ServiceState(image=REPO_AGENT, version="1.0", digest=agent_digest),
        ),
        window=UpdateWindow(
            from_=window_from, until=window_until,
            not_below_outdoor_temp_c=not_below_outdoor_temp_c,
        ),
    )


def _container(
    image_ref: str, *, running: bool = True, health: str | None = None
) -> dict[str, object]:
    state: dict[str, object] = {"Running": running}
    if health is not None:
        state["Health"] = {"Status": health}
    return {"Config": {}, "HostConfig": {}, "Image": image_ref, "State": state}


def _baseline_inspect() -> dict[str, dict[str, object]]:
    return {
        "thermoctl": _container(f"{REPO_THERMOCTL}@{OLD_THERMOCTL}"),
        "zigbee2mqtt": _container(f"{REPO_ZIGBEE}@{OLD_ZIGBEE}"),
        "mosquitto": _container(f"{REPO_MOSQUITTO}@{OLD_MOSQUITTO}"),
        "thermoctl-agent": _container(f"{REPO_AGENT}@{OLD_AGENT}"),
    }


def _baseline_images() -> dict[str, dict[str, object]]:
    return {
        f"{REPO_THERMOCTL}@{OLD_THERMOCTL}": {"RepoDigests": [f"{REPO_THERMOCTL}@{OLD_THERMOCTL}"]},
        f"{REPO_ZIGBEE}@{OLD_ZIGBEE}": {"RepoDigests": [f"{REPO_ZIGBEE}@{OLD_ZIGBEE}"]},
        f"{REPO_MOSQUITTO}@{OLD_MOSQUITTO}": {"RepoDigests": [f"{REPO_MOSQUITTO}@{OLD_MOSQUITTO}"]},
        f"{REPO_AGENT}@{OLD_AGENT}": {"RepoDigests": [f"{REPO_AGENT}@{OLD_AGENT}"]},
    }


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "pending": tmp_path / "pending_swap.json",
        "log": tmp_path / "agent.log",
        "watchdog": tmp_path / "watchdog-state.env",
    }


# --- the two owner-mandated, fail-closed rejections (2026-09-28) -----------


def test_reconcile_rejects_when_pilot_mode_is_false_nothing_pulled_no_backup(
    tmp_path: Path,
) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    backup_config = _backup_config(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=False,
            backup_config=backup_config,
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )

        assert outcome.successful is False
        assert "pilot_mode" in outcome.reason
        assert app.calls == []  # nothing pulled -- pre-check rejected first.

    assert "pilot_mode" in paths["log"].read_text(encoding="utf-8")
    assert not paths["pending"].exists()


@pytest.mark.parametrize("health_value", [None, "fault", "unknown"])
def test_reconcile_rejects_when_control_health_is_not_confirmed_ok(
    tmp_path: Path, health_value: str | None
) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    backup_config = _backup_config(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=backup_config,
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: health_value,
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )

        assert outcome.successful is False
        assert "health" in outcome.reason
        assert app.calls == []


def test_reconcile_rejects_when_outdoor_temperature_is_unreadable(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    backup_config = _backup_config(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=backup_config,
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: None,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )

        assert outcome.successful is False
        assert "outdoor temperature" in outcome.reason
        assert app.calls == []


# --- the rest of the pre-check ----------------------------------------------


def test_reconcile_rejects_when_outdoor_temperature_is_below_threshold(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL, not_below_outdoor_temp_c=0.0)
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: -3.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "below the update threshold" in outcome.reason
        assert app.calls == []


def test_reconcile_rejects_outside_the_time_window(tmp_path: Path) -> None:
    desired = _desired_state(
        thermoctl_digest=NEW_THERMOCTL, window_from=dt_time(9, 0), window_until=dt_time(10, 0)
    )
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW.replace(hour=12),
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "update window" in outcome.reason
        assert app.calls == []


def test_reconcile_rejects_when_free_disk_space_is_at_or_below_20_percent(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 20},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "free disk space" in outcome.reason
        assert app.calls == []


def test_reconcile_rejects_when_disk_usage_is_unreadable(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: None,
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "disk" in outcome.reason
        assert app.calls == []


def test_reconcile_precheck_passes_returns_none(tmp_path: Path) -> None:
    desired = _desired_state()
    assert (
        _reconcile_precheck(
            desired,
            pilot_mode=True,
            now=NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
        )
        is None
    )


def test_reconcile_already_at_desired_revision_is_a_no_op(tmp_path: Path) -> None:
    desired = _desired_state()  # every digest already matches the baseline.
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome == ReconcileOutcome(
            successful=True, reason="already at the desired revision."
        )
        # Only the (read-only) digest-comparison lookups happened, no pull.
        assert all("create" not in call and "stop" not in call for call in app.calls)


# --- digest format / hard-coded source -- security principle 2 -------------


def test_reconcile_rejects_missing_digest_prevents_start(tmp_path: Path) -> None:
    desired = _desired_state()
    # Bypasses `ServiceState`'s own model-level pattern to exercise
    # `reconcile_desired_state`'s own defense-in-depth check (security
    # principle 5: every check lives in `agent/`, enforced there too).
    tampered = ServiceState.model_construct(image=REPO_THERMOCTL, version="1.0", digest="")
    desired = desired.model_copy(
        update={"services": desired.services.model_copy(update={"thermoctl": tampered})}
    )
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "not a plain sha256 digest" in outcome.reason
        assert all("images/create" not in call for call in app.calls)


@pytest.mark.parametrize(
    "bad_image",
    [
        "ghcr.io/magicalwig34653/thermoctl-evil",
        "ghcr.io/magicalwig34653/thermoctl-agent",
        f"ghcr.io/magicalwig34653/thermoctl@{NEW_THERMOCTL}",
        "GHCR.IO/magicalwig34653/thermoctl",
        "ghcr.io/magicalwig34653/thermoctl/",
        "ghcr.io/magicalwig34653/thermoctl:latest",
        "evil.example.com/ghcr.io/magicalwig34653/thermoctl",
    ],
)
def test_reconcile_rejects_image_from_unlisted_source(tmp_path: Path, bad_image: str) -> None:
    desired = _desired_state()
    tampered = ServiceState(image=bad_image, version="1.0", digest=NEW_THERMOCTL)
    desired = desired.model_copy(
        update={"services": desired.services.model_copy(update={"thermoctl": tampered})}
    )
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "hard-coded source" in outcome.reason
        assert all("images/create" not in call for call in app.calls)


# --- backup failure aborts, nothing pulled ----------------------------------


def test_reconcile_aborts_when_backup_before_update_fails(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    backup_config = _backup_config(tmp_path)
    # An unreadable recipients file -- `run_before_update_backup` then
    # raises `RecipientsError` before a single byte is read.
    backup_config = backup_config.__class__(
        apartment_id=backup_config.apartment_id,
        agent_version=backup_config.agent_version,
        staging_dir=backup_config.staging_dir,
        thermoctl_db_path=backup_config.thermoctl_db_path,
        zigbee2mqtt_dir=backup_config.zigbee2mqtt_dir,
        client=backup_config.client,
        recipients_file=tmp_path / "does-not-exist.txt",
    )

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=backup_config,
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "backup before update failed" in outcome.reason
        assert all("images/create" not in call for call in app.calls)
    assert not paths["pending"].exists()


# --- pull / RepoDigests verification ----------------------------------------


def test_reconcile_aborts_when_pull_fails_old_state_stays(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(),
        images=_baseline_images(),
        pull_errors={(REPO_THERMOCTL, NEW_THERMOCTL): "manifest unknown"},
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "old state stays" in outcome.reason
        # The container itself was never touched.
        assert all(
            "stop" not in call and "containers/create" not in call for call in app.calls
        )


def test_reconcile_aborts_when_repo_digests_do_not_match_after_pull(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    images = _baseline_images()
    # No entry for the new digest -- simulates a pull that "succeeded" but
    # the daemon's own local image never actually carries that RepoDigest
    # (a mismatched/lying registry).

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=images
    ) as (socket_path, app):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "RepoDigests" in outcome.reason
        assert all(
            "stop" not in call and "containers/create" not in call for call in app.calls
        )


# --- successful swap, health confirmed immediately --------------------------


def test_reconcile_swaps_thermoctl_and_confirms_health(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }

    with run_fake_docker_api_with_app(inspect=_baseline_inspect(), images=images) as (
        socket_path,
        app,
    ):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
            health_deadline_s=5.0,
            poll_interval_s=0.01,
        )
        assert outcome.successful is True
        assert outcome.service == "thermoctl"
        assert app.inspect["thermoctl"]["Image"] == f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"
        assert app.inspect["thermoctl"]["State"]["Running"] is True
        # zigbee2mqtt (the "bigger risk", never touched in the same pass)
        # was left completely alone.
        assert app.inspect["zigbee2mqtt"]["Image"] == f"{REPO_ZIGBEE}@{OLD_ZIGBEE}"

    assert not paths["pending"].exists()


def test_reconcile_selects_only_one_service_when_two_differ(tmp_path: Path) -> None:
    """"one service per reconcile pass; never zigbee2mqtt together with
    thermoctl" (implementation plan, P5.4) -- both differ here, only
    `thermoctl` (first in `RECONCILE_SERVICE_ORDER`) is touched."""

    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL, zigbee_digest=NEW_ZIGBEE)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }
    images[f"{REPO_ZIGBEE}@{NEW_ZIGBEE}"] = {"RepoDigests": [f"{REPO_ZIGBEE}@{NEW_ZIGBEE}"]}

    with run_fake_docker_api_with_app(inspect=_baseline_inspect(), images=images) as (
        socket_path,
        app,
    ):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
            health_deadline_s=5.0,
            poll_interval_s=0.01,
        )
        assert outcome.successful is True
        assert outcome.service == "thermoctl"
        assert app.inspect["thermoctl"]["Image"] == f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"
        assert app.inspect["zigbee2mqtt"]["Image"] == f"{REPO_ZIGBEE}@{OLD_ZIGBEE}"
        assert not any(
            call.startswith("POST containers/zigbee2mqtt") for call in app.calls
        )


# --- no health within the deadline triggers rollback ------------------------


def test_reconcile_rolls_back_when_no_health_within_deadline(tmp_path: Path) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }
    inspect = _baseline_inspect()

    with run_fake_docker_api_with_app(inspect=inspect, images=images) as (socket_path, app):
        # The newly (re)created container never reports healthy -- forces
        # the poll loop to genuinely run out its own (small, real-time)
        # deadline instead of succeeding on the very first check.
        app.default_health_on_create = "unhealthy"

        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: datetime.now(UTC),
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
            health_deadline_s=0.1,
            poll_interval_s=0.02,
        )

        assert outcome.successful is False
        assert "rolling back" in outcome.reason
        assert app.inspect["thermoctl"]["Image"] == f"{REPO_THERMOCTL}@{OLD_THERMOCTL}"

    assert not paths["pending"].exists()
    assert "rolling back" in paths["log"].read_text(encoding="utf-8")


# --- restart resumes wait/rollback for a pending swap -----------------------


def test_reconcile_resumes_and_rolls_back_an_already_expired_pending_swap(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    inspect = _baseline_inspect()
    inspect["thermoctl"] = _container(
        f"{REPO_THERMOCTL}@{NEW_THERMOCTL}", health="unhealthy"
    )
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }

    # Simulates an agent restart mid-wait: a swap persisted well in the
    # past, before this call -- its 15-minute deadline (here, artificially
    # small) is already behind us.
    _save_pending_swap(
        paths["pending"],
        PendingSwap(
            service="thermoctl",
            container="thermoctl",
            repo=REPO_THERMOCTL,
            previous_digest=OLD_THERMOCTL,
            new_digest=NEW_THERMOCTL,
            since=NOW.timestamp() - 1000.0,
        ),
    )

    with run_fake_docker_api_with_app(inspect=inspect, images=images) as (socket_path, app):
        # A fresh `reconcile_desired_state` call, as if the agent process
        # had just restarted -- no pre-check, no backup, no pull happens
        # for this call at all: it only resumes the pending swap.
        outcome = reconcile_desired_state(
            _desired_state(thermoctl_digest=NEW_THERMOCTL),
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
            health_deadline_s=900.0,
            poll_interval_s=5.0,
        )

        assert outcome.successful is False
        assert "rolling back" in outcome.reason
        assert app.inspect["thermoctl"]["Image"] == f"{REPO_THERMOCTL}@{OLD_THERMOCTL}"
        # Resumed the pending swap, never ran the ordinary path again.
        assert all("images/create" not in call for call in app.calls)

    assert not paths["pending"].exists()


def test_reconcile_resumes_and_confirms_a_pending_swap_that_becomes_healthy(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    inspect = _baseline_inspect()
    inspect["thermoctl"] = _container(f"{REPO_THERMOCTL}@{NEW_THERMOCTL}", health="healthy")

    _save_pending_swap(
        paths["pending"],
        PendingSwap(
            service="thermoctl",
            container="thermoctl",
            repo=REPO_THERMOCTL,
            previous_digest=OLD_THERMOCTL,
            new_digest=NEW_THERMOCTL,
            since=NOW.timestamp(),
        ),
    )

    with run_fake_docker_api_with_app(inspect=inspect, images=_baseline_images()) as (
        socket_path,
        app,
    ):
        outcome = reconcile_desired_state(
            _desired_state(thermoctl_digest=NEW_THERMOCTL),
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
            health_deadline_s=900.0,
            poll_interval_s=5.0,
        )
        assert outcome.successful is True
        assert outcome.reason == "swap confirmed healthy."

    assert not paths["pending"].exists()


def test_load_pending_swap_returns_none_when_absent(tmp_path: Path) -> None:
    assert _load_pending_swap(tmp_path / "does-not-exist.json") is None


def test_save_and_load_pending_swap_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "pending_swap.json"
    swap = PendingSwap(
        service="mosquitto",
        container="mosquitto",
        repo=REPO_MOSQUITTO,
        previous_digest=OLD_MOSQUITTO,
        new_digest=OLD_MOSQUITTO,
        since=123.5,
    )
    _save_pending_swap(path, swap)
    loaded = _load_pending_swap(path)
    assert loaded == swap
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["service"] == "mosquitto"

    _save_pending_swap(path, None)
    assert not path.exists()
    assert _load_pending_swap(path) is None


# --- agent service: never swapped directly, handed to the watchdog ---------


def test_reconcile_agent_service_never_recreates_itself_hands_off_to_watchdog(
    tmp_path: Path,
) -> None:
    desired = _desired_state(agent_digest=NEW_AGENT)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_AGENT}@{NEW_AGENT}"] = {"RepoDigests": [f"{REPO_AGENT}@{NEW_AGENT}"]}

    with run_fake_docker_api_with_app(inspect=_baseline_inspect(), images=images) as (
        socket_path,
        app,
    ):
        outcome = reconcile_desired_state(
            desired,
            pilot_mode=True,
            backup_config=_backup_config(tmp_path),
            watchdog_state_path=paths["watchdog"],
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            now=lambda: NOW,
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )

        assert outcome.successful is True
        assert outcome.service == "agent"
        assert "watchdog" in outcome.reason
        # Never stopped/recreated its own container.
        assert all(
            "thermoctl-agent/stop" not in call and "containers/create" not in call
            for call in app.calls
        )

    watchdog_state = paths["watchdog"].read_text(encoding="utf-8")
    assert f"desired={NEW_AGENT}" in watchdog_state
    assert not paths["pending"].exists()


# --- the standalone Docker Engine API helpers, directly ---------------------


def test_pull_image_by_digest_raises_runtime_error_on_stream_error(tmp_path: Path) -> None:
    with run_fake_docker_api_with_app(
        pull_errors={(REPO_THERMOCTL, NEW_THERMOCTL): "manifest unknown"}
    ) as (socket_path, _app):
        with pytest.raises(RuntimeError, match="manifest unknown"):
            pull_image_by_digest(REPO_THERMOCTL, NEW_THERMOCTL, socket_path=socket_path)


def test_pull_image_by_digest_succeeds_when_no_error_configured(tmp_path: Path) -> None:
    with run_fake_docker_api_with_app() as (socket_path, app):
        pull_image_by_digest(REPO_THERMOCTL, NEW_THERMOCTL, socket_path=socket_path)
    assert any(call.startswith("POST images/create") for call in app.calls)


def test_image_repo_digests_returns_empty_list_without_repo_digests_key(tmp_path: Path) -> None:
    with run_fake_docker_api_with_app(images={"foo": {}}) as (socket_path, _app):
        assert image_repo_digests("foo", socket_path=socket_path) == []


def test_image_repo_digests_raises_on_missing_image() -> None:
    from tests.docker_api_support import unreachable_socket_path

    with pytest.raises(httpx.HTTPError):
        image_repo_digests("nope", socket_path=unreachable_socket_path())


def test_verify_pulled_digest_false_on_transport_error() -> None:
    from tests.docker_api_support import unreachable_socket_path

    assert (
        verify_pulled_digest(
            REPO_THERMOCTL, NEW_THERMOCTL, socket_path=unreachable_socket_path()
        )
        is False
    )


def test_current_repo_digest_none_when_container_missing() -> None:
    with run_fake_docker_api_with_app() as (socket_path, _app):
        assert current_repo_digest("thermoctl", REPO_THERMOCTL, socket_path=socket_path) is None


def test_current_repo_digest_none_when_no_matching_repo_digest(tmp_path: Path) -> None:
    inspect = {"thermoctl": _container("sha256-localid")}
    images = {"sha256-localid": {"RepoDigests": [f"{REPO_ZIGBEE}@{OLD_ZIGBEE}"]}}
    with run_fake_docker_api_with_app(inspect=inspect, images=images) as (socket_path, _app):
        assert current_repo_digest("thermoctl", REPO_THERMOCTL, socket_path=socket_path) is None


def test_container_is_healthy_none_when_container_missing() -> None:
    with run_fake_docker_api_with_app() as (socket_path, _app):
        assert container_is_healthy("thermoctl", socket_path=socket_path) is None


def test_container_is_healthy_true_when_running_and_no_healthcheck(tmp_path: Path) -> None:
    inspect = {"thermoctl": _container(f"{REPO_THERMOCTL}@{OLD_THERMOCTL}")}
    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        assert container_is_healthy("thermoctl", socket_path=socket_path) is True


def test_container_is_healthy_false_when_unhealthy(tmp_path: Path) -> None:
    inspect = {
        "thermoctl": _container(f"{REPO_THERMOCTL}@{OLD_THERMOCTL}", health="unhealthy")
    }
    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        assert container_is_healthy("thermoctl", socket_path=socket_path) is False


def test_select_service_to_update_none_when_all_match(tmp_path: Path) -> None:
    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, _app):
        assert _select_service_to_update(_desired_state(), socket_path=socket_path) is None


def test_await_or_rollback_uses_container_is_healthy_directly(tmp_path: Path) -> None:
    """A focused, non-`reconcile_desired_state` test of the wait/rollback
    helper's own success path -- confirms it clears the pending-swap file
    and never touches Docker beyond the health poll when already healthy
    on the first check."""

    paths = _paths(tmp_path)
    swap = PendingSwap(
        service="mosquitto",
        container="mosquitto",
        repo=REPO_MOSQUITTO,
        previous_digest=OLD_MOSQUITTO,
        new_digest=OLD_MOSQUITTO,
        since=NOW.timestamp(),
    )
    inspect = {"mosquitto": _container(f"{REPO_MOSQUITTO}@{OLD_MOSQUITTO}", health="healthy")}

    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, app):
        outcome = _await_or_rollback_pending_swap(
            swap,
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            socket_path=socket_path,
            health_deadline_s=5.0,
            poll_interval_s=0.01,
            sleep=lambda _s: None,
            now=lambda: NOW,
        )
        assert outcome.successful is True
        assert all("stop" not in call for call in app.calls)

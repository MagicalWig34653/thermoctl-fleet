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

import inspect
import json
import os
import time as time_module
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import httpx
import pytest
from pyrage import x25519

import agent.loop as agent_loop
from agent.loop import (
    BackupConfig,
    PendingSwap,
    ReconcileOutcome,
    _await_or_rollback_pending_swap,
    _default_health_reader,
    _default_outdoor_temp_reader,
    _load_pending_swap,
    _reconcile_precheck,
    _recreate_container_with_image,
    _rollback_to_previous,
    _save_pending_swap,
    _select_service_to_update,
    _time_within_update_window,
    container_is_healthy,
    current_repo_digest,
    image_repo_digests,
    pull_image_by_digest,
    reconcile_desired_state,
    verify_pulled_digest,
)
from agent.sources import ALLOWED_SOURCES
from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow
from tests.docker_api_support import run_fake_docker_api_with_app, unreachable_socket_path

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
    with pytest.raises(httpx.HTTPError):
        image_repo_digests("nope", socket_path=unreachable_socket_path())


def test_verify_pulled_digest_false_on_transport_error() -> None:
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


# --- cross-review fixes: pending-swap record validation ---------------------
# `_load_pending_swap` used to trust `service`/`previous_digest`/`new_digest`
# straight out of the JSON file; it no longer carries `container`/`repo` at
# all (see `PendingSwap`'s own docstring) and validates every field it does
# carry before constructing a `PendingSwap`, so a tampered or corrupted file
# is rejected -- fail closed, exactly like an unsafe path -- before
# `reconcile_desired_state` can ever resume it and touch Docker.


def test_load_pending_swap_rejects_unknown_service(tmp_path: Path) -> None:
    path = tmp_path / "pending_swap.json"
    path.write_text(
        json.dumps(
            {
                "service": "not-a-real-service",
                "previous_digest": OLD_THERMOCTL,
                "new_digest": NEW_THERMOCTL,
                "since": 1.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown or disallowed service"):
        _load_pending_swap(path)


def test_load_pending_swap_rejects_agent_service(tmp_path: Path) -> None:
    """`"agent"` is a real, known service name -- but never one this code
    itself persists a pending swap for (security principle 6: the agent
    never recreates its own container). A record naming it is therefore
    always tampered or corrupt, never genuine, and is rejected the same
    way an entirely unknown service name is."""

    path = tmp_path / "pending_swap.json"
    path.write_text(
        json.dumps(
            {
                "service": "agent",
                "previous_digest": OLD_AGENT,
                "new_digest": NEW_AGENT,
                "since": 1.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown or disallowed service"):
        _load_pending_swap(path)


@pytest.mark.parametrize(
    ("previous_digest", "new_digest"),
    [
        ("not-a-digest", NEW_THERMOCTL),
        (OLD_THERMOCTL, "latest"),
        ("", NEW_THERMOCTL),
        (OLD_THERMOCTL, "sha256:" + "g" * 64),
    ],
)
def test_load_pending_swap_rejects_malformed_digests(
    tmp_path: Path, previous_digest: str, new_digest: str
) -> None:
    path = tmp_path / "pending_swap.json"
    path.write_text(
        json.dumps(
            {
                "service": "thermoctl",
                "previous_digest": previous_digest,
                "new_digest": new_digest,
                "since": 1.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest"):
        _load_pending_swap(path)


def test_load_pending_swap_rejects_non_numeric_since(tmp_path: Path) -> None:
    path = tmp_path / "pending_swap.json"
    path.write_text(
        json.dumps(
            {
                "service": "thermoctl",
                "previous_digest": OLD_THERMOCTL,
                "new_digest": NEW_THERMOCTL,
                "since": "not-a-number",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="since"):
        _load_pending_swap(path)


def test_load_pending_swap_rejects_missing_field(tmp_path: Path) -> None:
    path = tmp_path / "pending_swap.json"
    path.write_text(json.dumps({"service": "thermoctl"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field"):
        _load_pending_swap(path)


def test_load_pending_swap_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "pending_swap.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        _load_pending_swap(path)


def test_load_pending_swap_ignores_extra_repo_and_container_keys(tmp_path: Path) -> None:
    """An old-format file (a previous version of `_save_pending_swap`
    persisted `repo`/`container` directly) -- both extra keys are ignored,
    never read back into the resulting `PendingSwap`, which no longer even
    has fields for them."""

    path = tmp_path / "pending_swap.json"
    path.write_text(
        json.dumps(
            {
                "service": "thermoctl",
                "container": "evil-container",
                "repo": "evil.example.com/foo/bar",
                "previous_digest": OLD_THERMOCTL,
                "new_digest": NEW_THERMOCTL,
                "since": 42.0,
            }
        ),
        encoding="utf-8",
    )
    swap = _load_pending_swap(path)
    assert swap == PendingSwap(
        service="thermoctl",
        previous_digest=OLD_THERMOCTL,
        new_digest=NEW_THERMOCTL,
        since=42.0,
    )
    assert not hasattr(swap, "repo")
    assert not hasattr(swap, "container")


def test_reconcile_resumes_a_tampered_pending_swap_file_raises_and_touches_no_docker(
    tmp_path: Path,
) -> None:
    """The integration path: a corrupt/tampered pending-swap file makes
    `reconcile_desired_state` raise before it does anything at all -- in
    particular, before any Docker call (the same "fails closed" contract
    `load_agent_state` already has for the executed-ids file)."""

    paths = _paths(tmp_path)
    paths["pending"].write_text(
        json.dumps(
            {"service": "does-not-exist", "previous_digest": "x", "new_digest": "y", "since": 1}
        ),
        encoding="utf-8",
    )

    with run_fake_docker_api_with_app(
        inspect=_baseline_inspect(), images=_baseline_images()
    ) as (socket_path, app):
        with pytest.raises(ValueError, match="unknown or disallowed service"):
            reconcile_desired_state(
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
            )
        assert app.calls == []


def test_reconcile_resumes_an_old_format_pending_swap_never_touches_the_foreign_names(
    tmp_path: Path,
) -> None:
    """A tampered/old-format file naming a foreign container and a
    foreign repository -- resumed successfully (the fields it does carry
    are valid), but every Docker call made while resuming it names only
    the agent's own fixed `thermoctl` container/repo, never
    `"evil-container"`/`"evil.example.com/foo/bar"`."""

    paths = _paths(tmp_path)
    paths["pending"].write_text(
        json.dumps(
            {
                "service": "thermoctl",
                "container": "evil-container",
                "repo": "evil.example.com/foo/bar",
                "previous_digest": OLD_THERMOCTL,
                "new_digest": NEW_THERMOCTL,
                "since": NOW.timestamp(),
            }
        ),
        encoding="utf-8",
    )
    inspect = _baseline_inspect()
    inspect["thermoctl"] = _container(f"{REPO_THERMOCTL}@{NEW_THERMOCTL}", health="healthy")

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
        )
        assert outcome.successful is True
        assert all("evil" not in call for call in app.calls)


# --- cross-review fixes: main-path swap-recreate failure / rollback --------


def test_reconcile_swap_recreate_fails_rollback_succeeds_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }

    monkeypatch.setattr(agent_loop, "_recreate_container_with_image", _raise_http_error)
    monkeypatch.setattr(agent_loop, "_rollback_to_previous", lambda *a, **k: True)

    with run_fake_docker_api_with_app(inspect=_baseline_inspect(), images=images) as (
        socket_path,
        _app,
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
        assert outcome.successful is False
        assert "swapping the container failed" in outcome.reason
        assert f"Rolled back to {OLD_THERMOCTL}" in outcome.reason
    assert not paths["pending"].exists()


def test_reconcile_swap_recreate_fails_rollback_also_fails_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    images = _baseline_images()
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }

    monkeypatch.setattr(agent_loop, "_recreate_container_with_image", _raise_http_error)
    monkeypatch.setattr(agent_loop, "_rollback_to_previous", lambda *a, **k: False)

    with run_fake_docker_api_with_app(inspect=_baseline_inspect(), images=images) as (
        socket_path,
        _app,
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
        assert outcome.successful is False
        assert "manual intervention required" in outcome.reason


def test_await_or_rollback_pending_swap_timeout_rollback_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    swap = PendingSwap(
        service="mosquitto",
        previous_digest=OLD_MOSQUITTO,
        new_digest=OLD_MOSQUITTO,
        since=NOW.timestamp(),
    )
    inspect = {"mosquitto": _container(f"{REPO_MOSQUITTO}@{OLD_MOSQUITTO}", health="unhealthy")}
    monkeypatch.setattr(agent_loop, "_rollback_to_previous", lambda *a, **k: False)

    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        outcome = _await_or_rollback_pending_swap(
            swap,
            pending_swap_path=paths["pending"],
            local_log_path=paths["log"],
            socket_path=socket_path,
            health_deadline_s=0.0,
            poll_interval_s=0.01,
            sleep=lambda _s: None,
            now=lambda: NOW,
        )
        assert outcome.successful is False
        assert "manual intervention required" in outcome.reason
    assert not paths["pending"].exists()


def _raise_http_error(*_args: object, **_kwargs: object) -> None:
    raise httpx.HTTPError("boom")


def test_reconcile_previous_digest_none_refuses_to_swap(tmp_path: Path) -> None:
    """The container is currently running an image with no `RepoDigests`
    entry for the source repository at all -- `current_repo_digest` (both
    for service *selection* and for this second, rollback-target lookup)
    returns `None`, and `reconcile_desired_state` refuses to swap without
    a rollback target, rather than swapping with nothing to roll back to."""

    desired = _desired_state(thermoctl_digest=NEW_THERMOCTL)
    paths = _paths(tmp_path)
    inspect = _baseline_inspect()
    inspect["thermoctl"] = _container("some-local-image-id")
    images = _baseline_images()
    images["some-local-image-id"] = {"RepoDigests": []}
    images[f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"] = {
        "RepoDigests": [f"{REPO_THERMOCTL}@{NEW_THERMOCTL}"]
    }

    with run_fake_docker_api_with_app(inspect=inspect, images=images) as (socket_path, app):
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
        assert "current running digest could not be determined" in outcome.reason
        assert all("stop" not in call and "containers/create" not in call for call in app.calls)


# --- cross-review fixes: further Docker Engine API helper coverage ---------


def test_pull_image_by_digest_tolerates_blank_and_malformed_lines(tmp_path: Path) -> None:
    raw = b"\n" + b"not json at all\n" + json.dumps({"status": "ok"}).encode() + b"\n"
    with run_fake_docker_api_with_app(pull_raw_body=raw) as (socket_path, _app):
        pull_image_by_digest(REPO_THERMOCTL, NEW_THERMOCTL, socket_path=socket_path)  # no raise


def test_current_repo_digest_none_when_image_field_missing(tmp_path: Path) -> None:
    inspect = {"thermoctl": {"Config": {}, "HostConfig": {}, "State": {"Running": True}}}
    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        assert current_repo_digest("thermoctl", REPO_THERMOCTL, socket_path=socket_path) is None


def test_current_repo_digest_none_when_image_field_not_a_string(tmp_path: Path) -> None:
    inspect = {
        "thermoctl": {"Config": {}, "HostConfig": {}, "Image": 12345, "State": {"Running": True}}
    }
    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        assert current_repo_digest("thermoctl", REPO_THERMOCTL, socket_path=socket_path) is None


def test_current_repo_digest_none_when_image_repo_digests_lookup_raises(tmp_path: Path) -> None:
    """The container's own `Image` points at a local id that is not
    present in the image store at all -- `image_repo_digests` itself
    raises `httpx.HTTPError`, and `current_repo_digest` turns that into
    `None` rather than propagating it."""

    inspect = {"thermoctl": _container("sha256-not-in-the-image-store")}
    with run_fake_docker_api_with_app(inspect=inspect, images={}) as (socket_path, _app):
        assert current_repo_digest("thermoctl", REPO_THERMOCTL, socket_path=socket_path) is None


def test_container_is_healthy_none_when_state_is_not_a_dict(tmp_path: Path) -> None:
    inspect = {"thermoctl": {"Config": {}, "HostConfig": {}, "Image": "x", "State": "running"}}
    with run_fake_docker_api_with_app(inspect=inspect) as (socket_path, _app):
        assert container_is_healthy("thermoctl", socket_path=socket_path) is None


@pytest.mark.parametrize("failing_step", ["stop", "remove", "start"])
def test_recreate_container_with_image_raises_on_unexpected_status(
    tmp_path: Path, failing_step: str
) -> None:
    inspect = {"thermoctl": _container(f"{REPO_THERMOCTL}@{OLD_THERMOCTL}")}
    with run_fake_docker_api_with_app(
        inspect=inspect, force_status={failing_step: 500}
    ) as (socket_path, _app):
        with pytest.raises(httpx.HTTPStatusError):
            _recreate_container_with_image(
                "thermoctl", f"{REPO_THERMOCTL}@{NEW_THERMOCTL}", socket_path=socket_path
            )


def test_rollback_to_previous_false_on_transport_error() -> None:
    assert (
        _rollback_to_previous(
            "thermoctl", REPO_THERMOCTL, OLD_THERMOCTL, socket_path=unreachable_socket_path()
        )
        is False
    )


def test_default_health_reader_returns_none() -> None:
    assert _default_health_reader() is None


def test_default_outdoor_temp_reader_returns_none() -> None:
    assert _default_outdoor_temp_reader() is None


# --- cross-review fixes: local time default, midnight-crossing windows -----


def test_time_within_update_window_ordinary_range() -> None:
    window_from = dt_time(9, 0)
    window_until = dt_time(16, 0)
    assert _time_within_update_window(dt_time(9, 0), window_from, window_until) is True
    assert _time_within_update_window(dt_time(12, 0), window_from, window_until) is True
    assert _time_within_update_window(dt_time(16, 0), window_from, window_until) is True
    assert _time_within_update_window(dt_time(8, 59), window_from, window_until) is False
    assert _time_within_update_window(dt_time(16, 1), window_from, window_until) is False


def test_time_within_update_window_crossing_midnight_both_sides() -> None:
    # An overnight window, 22:00 to 06:00.
    window_from = dt_time(22, 0)
    window_until = dt_time(6, 0)
    # Evening side (after `from_`, before midnight).
    assert _time_within_update_window(dt_time(23, 0), window_from, window_until) is True
    assert _time_within_update_window(dt_time(22, 0), window_from, window_until) is True
    # Morning side (after midnight, before `until`).
    assert _time_within_update_window(dt_time(0, 30), window_from, window_until) is True
    assert _time_within_update_window(dt_time(6, 0), window_from, window_until) is True
    # The daytime gap between the two halves is outside the window.
    assert _time_within_update_window(dt_time(6, 1), window_from, window_until) is False
    assert _time_within_update_window(dt_time(21, 59), window_from, window_until) is False
    assert _time_within_update_window(dt_time(12, 0), window_from, window_until) is False


def test_time_within_update_window_degenerate_equal_bounds() -> None:
    instant = dt_time(9, 0)
    assert _time_within_update_window(instant, instant, instant) is True
    assert _time_within_update_window(dt_time(9, 1), instant, instant) is False


def test_reconcile_accepts_an_update_inside_a_midnight_crossing_window(tmp_path: Path) -> None:
    desired = _desired_state(
        thermoctl_digest=NEW_THERMOCTL, window_from=dt_time(22, 0), window_until=dt_time(6, 0)
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
            now=lambda: NOW.replace(hour=23, minute=0),
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        # Rejected only for an unrelated reason (backup), never for the
        # time window -- proves the window check itself passed.
        assert "update window" not in outcome.reason


def test_reconcile_rejects_the_gap_of_a_midnight_crossing_window(tmp_path: Path) -> None:
    desired = _desired_state(
        thermoctl_digest=NEW_THERMOCTL, window_from=dt_time(22, 0), window_until=dt_time(6, 0)
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
            now=lambda: NOW.replace(hour=12, minute=0),
            health_reader=lambda: "ok",
            outdoor_temp_reader=lambda: 10.0,
            disk_usage_reader=lambda: {"total_bytes": 100, "free_bytes": 50},
            socket_path=socket_path,
        )
        assert outcome.successful is False
        assert "update window" in outcome.reason
        assert app.calls == []


def test_reconcile_default_now_uses_base_station_local_time_not_utc() -> None:
    """Proves the default `now=` factory is genuinely local-time-aware,
    not silently `datetime.now(UTC)` -- by actually changing the process's
    local timezone (`TZ` + `time.tzset()`, POSIX only) to one that is
    never at a zero UTC offset, and checking the produced value's own
    offset follows it. If this defaulted to UTC, `utcoffset()` would be
    zero regardless of `TZ`, and this assertion would fail."""

    if not hasattr(time_module, "tzset"):
        pytest.skip("time.tzset is not available on this platform.")

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time_module.tzset()
    try:
        default_now = inspect.signature(reconcile_desired_state).parameters["now"].default
        produced = default_now()
        assert produced.tzinfo is not None
        local_now = datetime.now().astimezone()
        assert produced.utcoffset() == local_now.utcoffset()
        # New York is UTC-4 (EDT) or UTC-5 (EST), never UTC+0 -- this would
        # fail if the default factory ignored the local timezone entirely.
        assert produced.utcoffset() != timedelta(0)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time_module.tzset()

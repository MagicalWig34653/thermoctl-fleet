"""Weekly fleet health guard: personas must never run against a fleet that cannot write.

Run 2 stopped being useful when the Docker VM disk filled up: the fleet answered 500 on
heartbeats and logins, but the runner only noticed persona-call failures. The runner now
calls an injected `health_check()` at the start of every week (and once when a run starts);
this module builds the real one from small injected probes so it can be tested.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

MIN_FREE_BYTES = 1024**3  # refuse to continue when the fleet data volume has less than 1 GiB
HEARTBEAT_MAX_AGE = timedelta(minutes=15)
FLEET_ROLES = frozenset({"landlord", "caretaker", "admin"})
FLEET_CONTAINER = "thermoctl-sim-fleet"


class LoginOutcome:
    """Result of the latest weekly login refresh, shared by the refresh hook and the guard."""

    def __init__(self) -> None:
        self.failed_roles: frozenset[str] = frozenset()

    def record_failure(self, roles: Sequence[str]) -> None:
        self.failed_roles = frozenset(roles)

    def record_success(self) -> None:
        self.failed_roles = frozenset()

    def fleet_outage(self) -> bool:
        """Every fleet role failed in the same refresh: the fleet itself is not answering."""
        return FLEET_ROLES <= self.failed_roles


def parse_df_avail(output: str) -> int | None:
    """Available bytes from `df -P` output (1024-byte blocks), None if it cannot be read."""
    lines = [line for line in output.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = " ".join(lines[1:]).split()  # a long filesystem name may wrap onto its own line
    if len(fields) < 6:
        return None
    try:
        return int(fields[3]) * 1024
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_health_check(
    *,
    healthz: Callable[[], str | None],
    heartbeats: Callable[[], Mapping[str, datetime | None]],
    now: Callable[[], datetime],
    silent: Callable[[], frozenset[str]],
    apartments: Sequence[str],
    disk_avail: Callable[[], int | None],
    logins: LoginOutcome,
) -> Callable[[], list[str]]:
    """`healthz` returns a problem text or None; `disk_avail` returns bytes or None (unknown)."""

    def check() -> list[str]:
        problems: list[str] = []
        problem = healthz()
        if problem:
            problems.append(problem)
        if logins.fleet_outage():
            problems.append(
                "the weekly login refresh failed for all fleet roles "
                "(landlord, caretaker, admin): the fleet is not answering logins"
            )
        # Heartbeats are written by the fleet: too few fresh ones means it cannot write.
        expected = [a for a in apartments if a not in silent()]
        try:
            seen = heartbeats()
        except Exception as exc:  # noqa: BLE001 - any read failure is itself a problem
            problems.append(f"cannot read heartbeats: {exc}")
        else:
            cutoff = _utc(now()) - HEARTBEAT_MAX_AGE
            stale = [a for a in expected if (last := seen.get(a)) is None or _utc(last) < cutoff]
            if stale:
                fresh = len(expected) - len(stale)
                problems.append(
                    f"only {fresh} of {len(expected)} expected apartments have a heartbeat "
                    f"within 15 virtual minutes (missing: {', '.join(stale)})"
                )
        free = disk_avail()
        if free is not None and free < MIN_FREE_BYTES:
            problems.append(
                f"fleet data volume has {free / 1024**3:.2f} GiB free, "
                f"need at least {MIN_FREE_BYTES // 1024**3} GiB"
            )
        return problems

    return check


def docker_disk_avail() -> int | None:  # pragma: no cover - runs docker
    """Free bytes on the fleet container's /data volume, None if it cannot be read."""
    try:
        done = subprocess.run(  # noqa: S603
            ["docker", "exec", FLEET_CONTAINER, "df", "-P", "/data"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_df_avail(done.stdout) if done.returncode == 0 else None

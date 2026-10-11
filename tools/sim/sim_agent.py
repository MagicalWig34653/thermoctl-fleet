"""Sim agent: sends heartbeats for one simulated apartment.

`python -m agent run` sends no heartbeats (collect/send raise
NotImplementedError), so the simulation brings its own driver. The payload is
built by constructing the real `protocol.heartbeat.Heartbeat` model, so a model
change breaks the sim loudly. Nothing outside that model is transmitted (no
room temperatures, setpoints, schedules, absence periods or tenant data).

Usage: python -m tools.sim.sim_agent --apartment ID --fleet URL --token-file F
                                     --state-dir D [--interval-s 5]

Faults: `<state-dir>/faults/<apartment>.json` (absent = healthy) overlays the
healthy defaults. Top-level keys `thermoctl`, `control`, `devices`, `system`
(objects, field-wise overlay) and `open_faults` (list of {kind, since, zone});
unknown keys are ignored.

Reactions to the environment (all through marker files under `<state-dir>/faults`,
because the real agent loop is not running in the sim):

- `<apartment>.version` (JSON `{"revision", "version"}`, written when the
  landlord sets a new desired state): the first tick that sees a changed marker
  only remembers it, the next tick "applies" it -- reports
  `desired-state/result` and from then on the new `thermoctl.version`.
  At process start the marker is adopted silently (the box already runs it).
- `<apartment>.backup` (JSON `{"id", "kind", "size"}`): a changed marker makes
  the agent upload one backup of that kind. The operational-data block is
  random bytes encrypted with the real `age` format to two throw-away
  recipients (nobody holds the identities); it is never real tenant data.
- the token file is re-read on every tick (a rotation rewrites it in place).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from protocol.backups import BackupKind
from protocol.desired_state import DesiredStateOutcomeReport
from protocol.heartbeat import Heartbeat
from protocol.version import PROTOCOL_VERSION
from tools.sim.http import allowed_hosts, check_url

log = logging.getLogger("sim_agent")

AGENT_ID = "sim-agent/1"
_SECTIONS = {
    "thermoctl": ("version", "reachable", "mode"),
    "control": ("last_decision", "zones", "zones_with_heat_demand", "zones_without_reading"),
    "devices": (
        "zigbee_bridge",
        "weakest_battery_percent",
        "worst_signal_quality",
        "silent_devices",
    ),
    "system": ("uptime_s", "memory_free_percent", "disk_free_percent", "clock_drift_s"),
}


def healthy_sections() -> dict[str, Any]:
    return {
        "thermoctl": {"version": "sim", "reachable": True, "mode": "armed"},
        "control": {
            "last_decision": "2026-01-01T00:00:00Z",
            "zones": 3,
            "zones_with_heat_demand": 1,
            "zones_without_reading": 0,
        },
        "devices": {
            "zigbee_bridge": "connected",
            "weakest_battery_percent": 100,
            "worst_signal_quality": 100,
            "silent_devices": 0,
        },
        "system": {
            "uptime_s": 3600,
            "memory_free_percent": 60,
            "disk_free_percent": 60,
            "clock_drift_s": 0.0,
        },
        "open_faults": [],
    }


class FaultFile:
    """Reads `<state_dir>/faults/<apartment>.json`; never raises."""

    def __init__(self, state_dir: Path, apartment: str) -> None:
        if not apartment or any(bad in apartment for bad in ("/", "\\", "..")):
            raise ValueError(f"invalid apartment id: {apartment!r}")
        self.path = state_dir / "faults" / f"{apartment}.json"
        self.apartment = apartment

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("fault file for %s unreadable: %s", self.apartment, type(exc).__name__)
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("fault file for %s is not valid JSON; treating as healthy", self.apartment)
            return {}
        if not isinstance(data, dict):
            log.warning("fault file for %s is not an object; treating as healthy", self.apartment)
            return {}
        return data


MAX_DRIFT_S = 86400.0  # one day: more would overflow timedelta and means nothing here


def read_drift(state_dir: Path, apartment: str) -> float:
    """Seconds this agent's clock is off (`<state>/faults/<apartment>.drift`).

    0.0 if unset, unreadable or non-finite; clamped to +/-MAX_DRIFT_S.
    """
    path = FaultFile(state_dir, apartment).path.with_suffix(".drift")
    try:
        value = float(path.read_text().strip())
    except (OSError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(-MAX_DRIFT_S, min(MAX_DRIFT_S, value))


def drifted_now(state_dir: Path, apartment: str) -> Callable[[], datetime]:
    """Clock of the agent: virtual time plus its drift (re-read on every call)."""
    return lambda: datetime.now(UTC) + timedelta(seconds=read_drift(state_dir, apartment))


def _overlay(base: dict[str, Any], faults: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: v for k, v in base.items()}
    for section, fields in _SECTIONS.items():
        patch = faults.get(section)
        if isinstance(patch, dict):
            out[section] = {**base[section], **{k: v for k, v in patch.items() if k in fields}}
    if isinstance(faults.get("open_faults"), list):
        out["open_faults"] = [
            {k: f[k] for k in ("kind", "since", "zone") if k in f}
            for f in faults["open_faults"]
            if isinstance(f, dict)
        ]
    return out


def build_heartbeat(apartment: str, health: dict[str, Any], sent_at: datetime) -> Heartbeat:
    return Heartbeat.model_validate(
        {
            "apartment": apartment,
            "sent_at": sent_at,
            "agent": AGENT_ID,
            "protocol_version": PROTOCOL_VERSION,
            **health,
        }
    )


def make_health(state_dir: Path, apartment: str) -> Callable[[], dict[str, Any]]:
    """Health factory: healthy defaults overlaid with the apartment's fault file.

    The file is re-read on every call. Content that does not validate against
    the protocol model falls back to healthy, logged at ERROR.
    """
    fault_file = FaultFile(state_dir, apartment)

    def health() -> dict[str, Any]:
        merged = _overlay(healthy_sections(), fault_file.read())
        drift = read_drift(state_dir, apartment)
        if drift:
            merged["system"] = {**merged["system"], "clock_drift_s": drift}
        try:
            build_heartbeat(apartment, merged, datetime.now(UTC))
        except ValidationError:
            log.error(
                "fault file %s for %s does not fit the heartbeat model; sending healthy",
                fault_file.path,
                apartment,
            )
            return healthy_sections()
        return merged

    return health


def read_marker(state_dir: Path, apartment: str, suffix: str) -> dict[str, Any] | None:
    """`<state>/faults/<apartment>.<suffix>` as a JSON object; None if absent or unusable."""
    path = FaultFile(state_dir, apartment).path.with_suffix(f".{suffix}")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_backup_body(kind: BackupKind, apartment: str, size: int, version: str) -> bytes:
    """One fictional backup body. Never real tenant data: random bytes / a tiny config.

    `operational_data`: `size` random bytes, encrypted in the real age format to two
    freshly generated recipients whose identities are dropped at once (the cloud
    only ever sees an opaque block; nobody can decrypt it). `device_config`: plain
    JSON without tenant relation, like section 15.1 describes it.
    """
    if kind is BackupKind.DEVICE_CONFIG:
        return json.dumps(
            {
                "apartment": apartment,
                "services": {"thermoctl": {"version": version}},
                "agent": {"id": AGENT_ID},
                "simulated": True,
            },
            sort_keys=True,
        ).encode()
    import pyrage

    recipients = [pyrage.x25519.Identity.generate().to_public() for _ in range(2)]
    return bytes(pyrage.encrypt(os.urandom(size), recipients))


class SimAgent:
    def __init__(
        self,
        apartment: str,
        client: httpx.Client,
        token: str,
        health: Callable[[], dict[str, Any]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_file: Path | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.apartment = apartment
        self._client = client
        self._token = token
        self._health = health
        self._now = now
        self._token_file = token_file
        self._state_dir = state_dir
        self._version: str | None = None
        self._pending_version: dict[str, Any] | None = None
        self._seen_revision: object = None
        self._seen_backup: object = None
        if state_dir is not None:
            marker = read_marker(state_dir, apartment, "version")
            if marker is not None:  # a restarted box already runs what was last installed
                self._seen_revision = marker.get("revision")
                version = marker.get("version")
                self._version = version if isinstance(version, str) else None
            backup = read_marker(state_dir, apartment, "backup")
            if backup is not None:  # a reboot does not repeat an old backup
                self._seen_backup = backup.get("id")

    def _reload_token(self) -> None:
        if self._token_file is None:
            return
        try:
            fresh = self._token_file.read_text().strip()
        except OSError:
            return
        if fresh:  # empty = mid-rewrite, keep the last good one
            self._token = fresh

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _apply_version_marker(self) -> None:
        if self._state_dir is None:
            return
        marker = read_marker(self._state_dir, self.apartment, "version")
        if marker is None:
            return
        if self._pending_version is not None:  # second tick: the "update" has finished
            pending, self._pending_version = self._pending_version, None
            version = pending.get("version")
            revision = pending.get("revision")
            if isinstance(version, str) and isinstance(revision, int):
                self._version = version
                self._seen_revision = revision
                self._post_desired_state_result(revision)
            return
        if marker.get("revision") != self._seen_revision:
            self._pending_version = marker

    def _post_desired_state_result(self, revision: int) -> None:
        report = DesiredStateOutcomeReport(
            revision=revision, successful=True, reason="sim: applied", service="thermoctl"
        )
        try:
            self._client.post(
                "/v1/desired-state/result",
                json=report.model_dump(mode="json"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            log.warning("desired-state result failed: %s", type(exc).__name__)

    def _run_backup_marker(self) -> None:
        if self._state_dir is None:
            return
        marker = read_marker(self._state_dir, self.apartment, "backup")
        if marker is None or marker.get("id") == self._seen_backup:
            return
        self._seen_backup = marker.get("id")
        size = marker.get("size")
        try:
            kind = BackupKind(str(marker.get("kind")))
            if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 2_000_000:
                raise ValueError("size")
        except ValueError:
            log.warning("backup marker for %s unusable; ignored", self.apartment)
            return
        body = build_backup_body(kind, self.apartment, size, self._version or "sim")
        try:
            resp = self._client.post(
                "/v1/backups",
                params={"kind": str(kind), "content_hash": hashlib.sha256(body).hexdigest()},
                content=body,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
            )
        except httpx.HTTPError as exc:
            log.warning("backup upload failed: %s", type(exc).__name__)
            return
        if not resp.is_success:
            log.warning("backup upload refused: HTTP %d", resp.status_code)

    def tick(self) -> bool:
        """Send one heartbeat. True on 2xx; never raises on transport/HTTP errors."""
        self._reload_token()
        self._apply_version_marker()
        self._run_backup_marker()
        try:
            health = self._health()
            if self._version is not None:
                health = {**health, "thermoctl": {**health["thermoctl"], "version": self._version}}
            hb = build_heartbeat(self.apartment, health, self._now())
        except ValidationError:
            log.warning("heartbeat for %s does not validate; not sent", self.apartment)
            return False
        try:
            resp = self._client.post(
                "/v1/heartbeat",
                json=hb.model_dump(mode="json"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            log.warning("heartbeat for %s failed: %s", self.apartment, type(exc).__name__)
            return False
        if not resp.is_success:
            log.warning("heartbeat for %s refused: HTTP %d", self.apartment, resp.status_code)
            return False
        return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m tools.sim.sim_agent")
    ap.add_argument("--apartment", required=True)
    ap.add_argument("--fleet", required=True)
    ap.add_argument("--token-file", required=True, type=Path)
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--interval-s", type=float, default=5.0)
    ap.add_argument(
        "--ca-file",
        type=Path,
        default=None,
        help="trust this CA for an https fleet (VM base stations); never disables verification",
    )
    ap.add_argument(
        "--pinned-fingerprint",
        default=None,
        help="sha256:<hex> of the fleet's leaf certificate, checked on top of --ca-file "
        "(same pin as agent-registration.json)",
    )
    return ap.parse_args(argv)


def build_http_client(args: argparse.Namespace) -> httpx.Client:
    """Plain http client (container agents), or, with `--ca-file`, the real agent's pinned
    TLS client (`agent.transport.build_client`): ordinary verification stays on and the leaf
    fingerprint must match. `--pinned-fingerprint` without `--ca-file` uses the system CAs."""
    if args.pinned_fingerprint is None and args.ca_file is None:
        return httpx.Client(
            base_url=args.fleet, follow_redirects=False, trust_env=False, timeout=10.0
        )
    if args.pinned_fingerprint is None:
        raise ValueError("--ca-file needs --pinned-fingerprint")
    from agent.transport import build_client

    return build_client(args.fleet, args.pinned_fingerprint, ca_file=args.ca_file, timeout=10.0)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
    max_ticks: int | None = None,
) -> int:
    args = parse_args(argv)
    try:
        check_url(args.fleet, allowed_hosts())
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        token = args.token_file.read_text().strip()
    except OSError as exc:
        print(f"error: cannot read token file: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not token:
        print("error: token file is empty", file=sys.stderr)
        return 2

    try:
        http = client or build_http_client(args)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    agent = SimAgent(
        args.apartment,
        http,
        token,
        make_health(args.state_dir, args.apartment),
        now=drifted_now(args.state_dir, args.apartment),
        token_file=args.token_file,
        state_dir=args.state_dir,
    )
    stop = threading.Event()
    if max_ticks is None:  # pragma: no cover - signal wiring, process-level
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    ticks = 0
    try:
        while not stop.is_set():
            agent.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            stop.wait(args.interval_s)
    finally:
        if client is None:  # pragma: no cover
            http.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())

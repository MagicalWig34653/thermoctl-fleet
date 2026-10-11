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
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

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
    the protocol model falls back to healthy, with a warning.
    """
    fault_file = FaultFile(state_dir, apartment)

    def health() -> dict[str, Any]:
        merged = _overlay(healthy_sections(), fault_file.read())
        try:
            build_heartbeat(apartment, merged, datetime.now(UTC))
        except ValidationError:
            log.warning("fault file for %s does not fit the heartbeat model; healthy", apartment)
            return healthy_sections()
        return merged

    return health


class SimAgent:
    def __init__(
        self,
        apartment: str,
        client: httpx.Client,
        token: str,
        health: Callable[[], dict[str, Any]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.apartment = apartment
        self._client = client
        self._token = token
        self._health = health
        self._now = now

    def tick(self) -> bool:
        """Send one heartbeat. True on 2xx; never raises on transport/HTTP errors."""
        try:
            hb = build_heartbeat(self.apartment, self._health(), self._now())
        except ValidationError:
            log.warning("heartbeat for %s does not validate; not sent", self.apartment)
            return False
        try:
            resp = self._client.post(
                "/v1/heartbeat",
                json=hb.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {self._token}"},
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
    return ap.parse_args(argv)


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

    http = client or httpx.Client(
        base_url=args.fleet, follow_redirects=False, trust_env=False, timeout=10.0
    )
    agent = SimAgent(args.apartment, http, token, make_health(args.state_dir, args.apartment))
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

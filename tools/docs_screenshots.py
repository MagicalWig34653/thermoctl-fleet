"""Seeds a throwaway fleet database with realistic demo data and captures
screenshots of the rebuilt UI (`docs/ui-redesign/`).

Reproducible, local-only tooling: a fresh SQLite file in a temporary
directory, migrated with the project's own Alembic chain (`fleet.storage
.upgrade`), a real `uvicorn` server started against it, and a real browser
(Playwright/Chromium) driving the real login flow (password + TOTP, computed
with `pyotp` from the secret `python -m fleet.admin create-user` would also
print). No secrets, no real apartment data -- every apartment id, address and
reading below is fictional (Lindenstraße 12, Gartenweg 8, Parkallee 3 in Musterstadt).

Run from the repository root, with Playwright's Chromium already installed
in the active environment (`playwright install chromium`):

    python tools/docs_screenshots.py

Writes PNGs into `docs/ui-redesign/` (light theme, 1440 and 390 px).
`tests/test_docs_screenshots.py` covers
the parts that are worth covering for real: `seed()` against a real,
migrated temporary SQLite database (read back through the actual
`fleet.storage`/UI view-builder API -- this is the part that breaks when
either changes shape), `create_ui_user()` against the real
`python -m fleet.admin create-user` subprocess, `main()`'s own
orchestration (every function it calls monkeypatched to a recorder,
asserting call order/arguments and that the server subprocess is always
torn down), `optimize_images()` against a real tiny PNG (skipped where
Pillow, an ad-hoc-only dependency for this script, is absent), and every
pure helper (`_free_port`, `_parse_totp_secret`,
`_seconds_until_next_totp_step`, `_webp_path_for`). Only `capture()` and
`start_server()` stay `# pragma: no cover`, each with its own reason --
they need a real browser (Playwright/Chromium) or a real, long-running
`uvicorn` server subprocess, which a unit test would only re-mock, not
exercise for real.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = REPO_ROOT / "docs" / "ui-redesign"


@dataclass(frozen=True)
class DemoProperty:
    name: str
    address: str


@dataclass(frozen=True)
class DemoApartment:
    id: str
    label: str
    floor: str
    orientation: str
    heating_circuits: int
    state: str
    property_index: int = 0


# Fictional throughout -- no real apartment, tenant, or address. The shape
# follows the owner's design draft: three properties with 6 / 6 / 4
# apartments, almost all of them reachable.
PROPERTIES = [
    DemoProperty("Lindenstraße 12", "Lindenstraße 12, 12345 Musterstadt"),
    DemoProperty("Gartenweg 8", "Gartenweg 8, 12345 Musterstadt"),
    DemoProperty("Parkallee 3", "Parkallee 3, 12345 Musterstadt"),
]

_FLOORS = ["EG", "EG", "1. OG", "1. OG", "2. OG", "2. OG"]
_ORIENTATIONS = ["Ost", "West", "Süd", "Nord", "Ost", "West"]


def _build_apartments() -> list[DemoApartment]:
    slugs = ["lindenstr12", "gartenweg8", "parkallee3"]
    counts = [6, 6, 4]
    entries = []
    for property_index, (slug, count) in enumerate(zip(slugs, counts, strict=True)):
        for number in range(1, count + 1):
            entries.append(
                DemoApartment(
                    id=f"{slug}-w{number:02d}",
                    label=f"Wohnung {number:02d}",
                    floor=_FLOORS[number - 1],
                    orientation=_ORIENTATIONS[number - 1],
                    heating_circuits=2 + number % 3,
                    state="occupied",
                    property_index=property_index,
                )
            )
    return entries


APARTMENTS = _build_apartments()

# Indices into `APARTMENTS` used by name below, so a reorder of the list
# above cannot silently point `seed()` at the wrong apartment.
_OK = 0  # Lindenstraße 12, Wohnung 01 -- healthy
_ALARM = 2  # Lindenstraße 12, Wohnung 03 -- not reporting (open alarm)
_FAULT = 5  # Lindenstraße 12, Wohnung 06 -- open sensor fault
_BATTERY = 7  # Gartenweg 8, Wohnung 02 -- weak battery
_ROLLOUT = range(6, 12)  # Gartenweg 8 -- the running rollout's apartments
_RECOVERED = 14  # Parkallee 3, Wohnung 03 -- was unreachable, came back
_FRESH_BACKUP = 15  # Parkallee 3, Wohnung 04 -- backup a few minutes ago


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_for_server(url: str, timeout_s: float = 20.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server at {url} did not come up in time.")


def seed(database_url: str) -> None:
    """Builds the demo fleet in the shape of the design draft: three
    properties (6 / 6 / 4 apartments), fresh heartbeats for all but one
    apartment, one sensor fault, one weak battery, a recent backup for
    every apartment, a recovered outage for the activity feed and one
    running rollout at partial progress."""

    from fleet.storage import create_storage, upgrade
    from protocol.events import Event
    from protocol.heartbeat import (
        ControlState,
        DeviceState,
        FaultKind,
        Heartbeat,
        OpenFault,
        SystemState,
        ThermoctlState,
    )
    from protocol.version import PROTOCOL_VERSION

    upgrade(database_url)
    storage = create_storage(database_url)

    property_ids = [
        storage.create_property(name=prop.name, address=prop.address).id for prop in PROPERTIES
    ]

    now = datetime.now(UTC)
    for entry in APARTMENTS:
        storage.create_apartment(
            entry.id,
            property_id=property_ids[entry.property_index],
            label=entry.label,
            floor=entry.floor,
            orientation=entry.orientation,
            state=entry.state,
            heating_circuits=entry.heating_circuits,
            pilot_mode=False,
        )

    def heartbeat(
        open_faults: list[OpenFault], weakest_battery: int, worst_signal: int
    ) -> Heartbeat:
        return Heartbeat(
            apartment="placeholder",
            protocol_version=PROTOCOL_VERSION,
            sent_at=now - timedelta(seconds=30),
            agent="0.3.0",
            thermoctl=ThermoctlState(version="0.9.5", reachable=True, mode="armed"),
            control=ControlState(
                last_decision=now - timedelta(minutes=2),
                zones=6,
                zones_with_heat_demand=2,
                zones_without_reading=0,
            ),
            devices=DeviceState(
                zigbee_bridge="connected",
                weakest_battery_percent=weakest_battery,
                worst_signal_quality=worst_signal,
                silent_devices=0,
            ),
            system=SystemState(
                uptime_s=962114, memory_free_percent=41, disk_free_percent=68, clock_drift_s=0.4
            ),
            open_faults=open_faults,
        )

    for index, entry in enumerate(APARTMENTS):
        if index == _ALARM:
            continue  # handled below: old heartbeat plus an open alarm
        faults: list[OpenFault] = []
        battery = 55 + (index * 7) % 40
        if index == _FAULT:
            faults = [
                OpenFault(kind=FaultKind.SENSOR_FAULT, since=now - timedelta(hours=2), zone="Bad")
            ]
        if index == _BATTERY:
            battery = 12
        storage.save_heartbeat(
            entry.id,
            heartbeat(faults, weakest_battery=battery, worst_signal=45 + (index * 5) % 40),
            received_at=now - timedelta(seconds=20 + index),
        )

    # The one apartment that stopped reporting: last heartbeat old enough
    # for the "not reporting" alarm.
    storage.save_heartbeat(
        APARTMENTS[_ALARM].id,
        heartbeat([], weakest_battery=66, worst_signal=60),
        received_at=now - timedelta(minutes=24),
    )
    storage.raise_alarm(
        APARTMENTS[_ALARM].id,
        kind="not_reporting",
        urgency="high",
        raised_at=now - timedelta(minutes=18),
    )

    # An outage that already ended -- a "Basisstation wieder erreichbar"
    # line in the activity feed.
    recovered = storage.raise_alarm(
        APARTMENTS[_RECOVERED].id,
        kind="not_reporting",
        urgency="high",
        raised_at=now - timedelta(hours=3),
    )
    if recovered is not None:
        storage.clear_alarm(recovered.id, now - timedelta(minutes=40))

    # A fault report arriving for the apartment with the open fault.
    storage.save_event(
        APARTMENTS[_FAULT].id,
        Event(schluessel="zigbee2mqtt:bridge", schwere="stoerung", titel="t", text="x"),
        now - timedelta(hours=2),
    )

    # A backup for every apartment, all younger than 24 hours; one only a
    # few minutes old.
    for index, entry in enumerate(APARTMENTS):
        age = timedelta(minutes=6) if index == _FRESH_BACKUP else timedelta(hours=1 + index)
        storage.create_backup_record(
            entry.id,
            "operational_data",  # type: ignore[arg-type]
            size_bytes=4_194_304,
            content_hash="sha256:" + "ab" * 32,
            storage_path="demo/not-a-real-path.age",
            now=now - age,
        )

    from datetime import time as dt_time

    from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow

    window = UpdateWindow(from_=dt_time(2, 0), until=dt_time(5, 0), not_below_outdoor_temp_c=-10.0)
    services = Services(
        thermoctl=ServiceState(
            image="ghcr.io/example/thermoctl", version="0.9.5", digest="sha256:" + "11" * 32
        ),
        zigbee2mqtt=ServiceState(
            image="ghcr.io/example/zigbee2mqtt", version="1.35.0", digest="sha256:" + "22" * 32
        ),
        mosquitto=ServiceState(
            image="ghcr.io/example/mosquitto", version="2.0.18", digest="sha256:" + "33" * 32
        ),
        agent=ServiceState(
            image="ghcr.io/example/thermoctl-agent", version="0.3.0", digest="sha256:" + "44" * 32
        ),
    )
    rollout_apartments = [APARTMENTS[index] for index in _ROLLOUT]
    for entry in rollout_apartments:
        storage.create_desired_state_revision(
            entry.id,
            DesiredState(revision=0, services=services, window=window),
            ui_username="demo",
            reason="Demo-Ausgangszustand für die Dokumentations-Screenshots.",
            now=now - timedelta(days=1),
        )

    # A running rollout at partial progress: four of six apartments done,
    # the fifth in progress, the last still queued.
    rollout = storage.create_rollout(
        service="thermoctl",
        version="0.9.6",
        digest="sha256:" + "cd" * 32,
        apartment_ids=[entry.id for entry in rollout_apartments],
        stagger_hours=2.0,
        timeout_hours=6.0,
        ui_username="demo",
        reason="Demo-Rollout für die Dokumentations-Screenshots.",
        now=now - timedelta(hours=6),
    )
    for position, entry in enumerate(rollout_apartments[:5]):
        storage.start_rollout_apartment(
            rollout.id,
            entry.id,
            revision=1,
            now=now - timedelta(minutes=5)
            if position == 4
            else now - timedelta(hours=4 - position),
        )
        if position < 4:
            storage.mark_rollout_apartment_converged(
                rollout.id, entry.id, now=now - timedelta(hours=3 - position * 0.5)
            )
    storage.set_rollout_pilot_converged(rollout.id, now - timedelta(hours=4))


def create_ui_user(database_url: str, totp_key: str, username: str, password: str) -> str:
    """Runs the real `python -m fleet.admin create-user` CLI (never a
    direct storage call for the account itself) and returns the TOTP
    secret parsed from its one-time provisioning-URI output."""

    env = dict(os.environ, FLEET_DATABASE_URL=database_url, FLEET_TOTP_KEY=totp_key)
    # Fixed argv, no shell, every element either a constant or a value this
    # same script generated (port, tempdir path) -- not untrusted input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "fleet.admin", "create-user", username],
        input=f"{password}\n{password}\n",
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"create-user failed (exit {result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return _parse_totp_secret(result.stdout)


def _parse_totp_secret(stdout: str) -> str:
    """Pulls the TOTP secret out of `fleet.admin create-user`'s one-time
    provisioning-URI line (`otpauth://totp/...?secret=XXXX&issuer=...`) --
    split out from `create_ui_user` so the parsing itself (not the
    subprocess call) can be asserted on directly."""

    for line in stdout.splitlines():
        if "secret=" in line:
            for part in line.split("?", 1)[-1].split("&"):
                if part.startswith("secret="):
                    return part[len("secret=") :]
    raise RuntimeError(f"Could not find TOTP secret in admin output:\n{stdout}")


# Starts a real `uvicorn` server subprocess and hands back the live
# process -- a unit test would only re-mock `subprocess.Popen`, not
# exercise anything for real; see the module docstring.
def start_server(database_url: str, port: int) -> subprocess.Popen[bytes]:  # pragma: no cover
    env = dict(
        os.environ,
        FLEET_DATABASE_URL=database_url,
        FLEET_TOTP_KEY=os.environ["FLEET_TOTP_KEY"],
        FLEET_WEBAUTHN_RP_ID="localhost",
        FLEET_WEBAUTHN_ORIGIN=f"http://localhost:{port}",
    )
    # Fixed argv, no shell -- see create_ui_user's own noqa for the same reasoning.
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "fleet.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process


def _seconds_until_next_totp_step(now: float, step: int = 30) -> float:
    """How long to sleep from wall-clock time `now` (`time.time()`) until
    the *next* TOTP step boundary, plus a one-second safety margin -- pure
    arithmetic, split out of `_wait_for_fresh_totp_window` so it can be
    asserted on for fixed `now` values without a real clock or a real
    sleep."""

    return (step - (int(now) % step)) + 1


def _wait_for_fresh_totp_window(
    clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None
) -> None:
    # The fleet rejects a reused TOTP code as a replay (same 30 s step
    # already consumed by the previous login in this script) -- each
    # capture pass logs in fresh, so each must land in its own step.
    # `clock`/`sleep` default to the real `time.time`/`time.sleep`; a test
    # injects fakes instead.
    clock_fn = clock if clock is not None else time.time
    sleep_fn = sleep if sleep is not None else time.sleep
    sleep_fn(_seconds_until_next_totp_step(clock_fn()))


def _webp_path_for(png_path: Path) -> Path:
    """The WebP sibling path `optimize_images` writes next to each
    captured PNG -- same stem, `.webp` suffix, same directory. Split out as
    its own function so the path decision can be asserted on without
    touching Pillow or the filesystem."""

    return png_path.with_suffix(".webp")


# Pages of the rebuilt UI captured for docs/ui-redesign/ (light theme only,
# at both widths): `(path, filename stem)`. Pages that still run on the
# legacy stylesheet (apartment detail, Einrichtung, Updates, Konto) are
# left out until phase 2 rebuilds them.
_REBUILT_PAGES: tuple[tuple[str, str], ...] = (
    ("/ui/", "uebersicht"),
    ("/ui/?ansicht=liste", "uebersicht-liste"),
    ("/ui/apartments", "wohnungen"),
    ("/ui/tasks", "aufgaben"),
)

_VIEWPORTS: tuple[int, ...] = (1440, 390)


def _shoot_full_page(page: Any, path: Path, width: int) -> None:  # pragma: no cover
    """Full-page screenshot: resize the viewport to the page's scrolled
    height first, then capture without Playwright's scroll-and-stitch (which
    would repaint a fixed element once per stitched slice)."""

    page.set_viewport_size({"width": width, "height": 900})
    height = page.evaluate("document.documentElement.scrollHeight")
    page.set_viewport_size({"width": width, "height": max(int(height), 900)})
    page.screenshot(path=str(path))


# Drives a real browser (Playwright/Chromium) against a real, already
# running fleet server -- a unit test would only re-mock Playwright.
def capture(  # pragma: no cover
    base_url: str, username: str, password: str, totp_secret: str
) -> None:
    import pyotp
    from playwright.sync_api import sync_playwright

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    totp = pyotp.TOTP(totp_secret)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for index, width in enumerate(_VIEWPORTS):
            if index > 0:
                _wait_for_fresh_totp_window()
            context = browser.new_context(viewport={"width": width, "height": 900})
            page = context.new_page()

            page.goto(f"{base_url}/ui/login", wait_until="networkidle")
            _shoot_full_page(page, IMG_DIR / f"login-{width}.png", width)

            # Step 1 -> step 2 of the one login form (client-side switch).
            page.fill("#username", username)
            page.fill("#password", password)
            page.click("#step-next")
            page.keyboard.type("123")
            _shoot_full_page(page, IMG_DIR / f"login-2fa-{width}.png", width)

            page.fill("#totp_code", "")
            page.keyboard.type(totp.now())  # auto-submits after six digits
            page.wait_for_url("**/ui/", timeout=10_000)

            for path, stem in _REBUILT_PAGES:
                page.goto(f"{base_url}{path}", wait_until="networkidle")
                _shoot_full_page(page, IMG_DIR / f"{stem}-{width}.png", width)

            if width <= 650:
                page.goto(f"{base_url}/ui/", wait_until="networkidle")
                page.set_viewport_size({"width": width, "height": 844})
                page.click(".mobile-menu")
                page.wait_for_timeout(400)
                page.screenshot(path=str(IMG_DIR / f"menu-{width}.png"))

            context.close()
        browser.close()


def optimize_images() -> None:
    """Re-saves every captured PNG optimized, and adds a WebP copy next to
    it. Needs Pillow, which is only installed ad hoc for this script
    (pyproject.toml's own mypy override comment above says so) -- not a
    project dependency, so `tests/test_docs_screenshots.py` skips its one
    real test here (`pytest.importorskip("PIL")`) in an environment where
    it is absent, rather than mocking it."""

    from PIL import Image

    for png_path in sorted(IMG_DIR.glob("*.png")):
        with Image.open(png_path) as img:
            img.save(png_path, optimize=True)
            img.convert("RGB").save(_webp_path_for(png_path), quality=82, method=6)


# Orchestrates seed() -> create_ui_user() -> start_server() -> capture() ->
# optimize_images() -- every one of those names is itself a module-level
# function this module's own call sites use unqualified, so
# `tests/test_docs_screenshots.py` exercises this function for real by
# monkeypatching each of them to a recorder and asserting the call order,
# arguments, and that the server subprocess is always torn down (even when
# `capture` raises, and killed if a clean `wait()` times out) -- no pragma
# needed, nothing here is actually untestable.
def main(argv: list[str] | None = None) -> int:
    import argparse
    import base64
    import secrets as _secrets

    argparse.ArgumentParser(
        description=(
            "Seed a throwaway demo fleet, start it, and capture the rebuilt UI "
            f"(light, 1440 and 390 px) into {IMG_DIR}."
        )
    ).parse_args([] if argv is None else argv)

    totp_key = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    os.environ["FLEET_TOTP_KEY"] = totp_key

    with tempfile.TemporaryDirectory(prefix="thermoctl-fleet-docs-") as tmp:
        database_url = f"sqlite:///{tmp}/fleet-docs-demo.db"
        seed(database_url)

        username, password = "demo", "demo-password-not-real-123"
        totp_secret = create_ui_user(database_url, totp_key, username, password)

        port = _free_port()
        server = start_server(database_url, port)
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_for_server(f"{base_url}/healthz")
            capture(base_url, username, password, totp_secret)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    optimize_images()
    print(f"Screenshots written to {IMG_DIR}")
    return 0


if __name__ == "__main__":  # pragma: no cover -- just an entry point
    raise SystemExit(main(sys.argv[1:]))

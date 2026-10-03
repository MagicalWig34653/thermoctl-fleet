"""Seeds a throwaway fleet database with realistic demo data and captures
screenshots of the real UI for the documentation website (`site/`).

Reproducible, local-only tooling: a fresh SQLite file in a temporary
directory, migrated with the project's own Alembic chain (`fleet.storage
.upgrade`), a real `uvicorn` server started against it, and a real browser
(Playwright/Chromium) driving the real login flow (password + TOTP, computed
with `pyotp` from the secret `python -m fleet.admin create-user` would also
print). No secrets, no real apartment data -- every apartment id, address and
reading below is fictional ("Musterstraße ...").

Run from the repository root, with Playwright's Chromium already installed
in the active environment (`playwright install chromium`):

    python tools/docs_screenshots.py

Writes PNGs into `site/assets/img/`. `tests/test_docs_screenshots.py` covers
the parts that are worth covering for real: `seed()` against a real,
migrated temporary SQLite database (read back through the actual
`fleet.storage`/UI view-builder API -- this is the part that breaks when
either changes shape), `create_ui_user()` against the real
`python -m fleet.admin create-user` subprocess, and every pure helper
(`_free_port`, `_parse_totp_secret`, `_seconds_until_next_totp_step`,
`_webp_path_for`). The parts that genuinely need a real browser or a real
`uvicorn` server (`capture()`, `start_server()`, `main()`) are
`# pragma: no cover` there, with a reason each -- a unit test for those
would only re-mock Playwright and `subprocess`, not exercise anything this
script doesn't already exercise for real.
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
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = REPO_ROOT / "site" / "assets" / "img"


@dataclass(frozen=True)
class DemoApartment:
    id: str
    label: str
    floor: str
    orientation: str
    heating_circuits: int
    state: str


# Fictional throughout -- no real apartment, tenant, or address.
APARTMENTS = [
    DemoApartment(
        id="musterstr1-we3",
        label="Musterstraße 1, WE 3",
        floor="2. OG",
        orientation="Süd",
        heating_circuits=4,
        state="ok",
    ),
    DemoApartment(
        id="musterstr1-we5",
        label="Musterstraße 1, WE 5",
        floor="3. OG",
        orientation="West",
        heating_circuits=3,
        state="ok",
    ),
    DemoApartment(
        id="beispielweg9-we1",
        label="Beispielweg 9, WE 1",
        floor="EG",
        orientation="Ost",
        heating_circuits=5,
        state="ok",
    ),
]


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
    """Builds the demo fleet: a property, three apartments, heartbeats (one
    with an open fault), an alarm, a backup, and a rollout."""

    from fleet.storage import create_storage, upgrade
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

    prop = storage.create_property(
        name="Musterstraße 1 & Beispielweg 9",
        address="Musterstraße 1 / Beispielweg 9, 12345 Musterstadt",
        notes="Demo-Bestand für die Dokumentations-Screenshots.",
    )

    now = datetime.now(UTC)
    for entry in APARTMENTS:
        storage.create_apartment(
            entry.id,
            property_id=prop.id,
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
            sent_at=now - timedelta(minutes=1),
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

    # Apartment 1: healthy, recent heartbeat.
    storage.save_heartbeat(
        APARTMENTS[0].id,
        heartbeat([], weakest_battery=78, worst_signal=61),
        received_at=now,
    )

    # Apartment 2: one open fault (sensor fault in the bathroom).
    storage.save_heartbeat(
        APARTMENTS[1].id,
        heartbeat(
            [OpenFault(kind=FaultKind.SENSOR_FAULT, since=now - timedelta(hours=5), zone="Bad")],
            weakest_battery=22,
            worst_signal=38,
        ),
        received_at=now,
    )

    # Apartment 3: last heartbeat is old enough to trigger the "not reporting" alarm.
    storage.save_heartbeat(
        APARTMENTS[2].id,
        heartbeat([], weakest_battery=55, worst_signal=70),
        received_at=now - timedelta(minutes=20),
    )
    storage.raise_alarm(
        APARTMENTS[2].id,
        kind="not_reporting",
        urgency="high",
        raised_at=now - timedelta(minutes=14),
    )

    # A backup for apartment 1.
    storage.create_backup_record(
        APARTMENTS[0].id,
        "operational_data",  # type: ignore[arg-type]
        size_bytes=4_194_304,
        content_hash="sha256:" + "ab" * 32,
        storage_path="demo/not-a-real-path.age",
        now=now,
    )

    from datetime import time as dt_time

    from protocol.desired_state import DesiredState, Services, ServiceState, UpdateWindow

    window = UpdateWindow(
        from_=dt_time(2, 0), until=dt_time(5, 0), not_below_outdoor_temp_c=-10.0
    )
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
    for entry in APARTMENTS[:2]:
        storage.create_desired_state_revision(
            entry.id,
            DesiredState(revision=0, services=services, window=window),
            ui_username="demo",
            reason="Demo-Ausgangszustand für die Dokumentations-Screenshots.",
            now=now,
        )

    # A rollout across two apartments.
    storage.create_rollout(
        service="thermoctl",
        version="0.9.6",
        digest="sha256:" + "cd" * 32,
        apartment_ids=[APARTMENTS[0].id, APARTMENTS[1].id],
        stagger_hours=2.0,
        timeout_hours=1.0,
        ui_username="demo",
        reason="Demo-Rollout für die Dokumentations-Screenshots.",
        now=now,
    )


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


# Drives a real browser (Playwright/Chromium) against a real, already
# running fleet server -- a unit test would only re-mock Playwright, not
# exercise anything for real; see the module docstring.
def capture(  # pragma: no cover
    base_url: str, username: str, password: str, totp_secret: str
) -> None:
    import pyotp
    from playwright.sync_api import sync_playwright

    IMG_DIR.mkdir(parents=True, exist_ok=True)

    totp = pyotp.TOTP(totp_secret)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        schemes: tuple[Literal["light", "dark"], ...] = ("light", "dark")
        for index, scheme in enumerate(schemes):
            if index > 0:
                _wait_for_fresh_totp_window()

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                color_scheme=scheme,
            )
            page = context.new_page()

            # Login page, unauthenticated.
            page.goto(f"{base_url}/ui/login", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"login-{scheme}.png"))

            page.fill("#username", username)
            page.fill("#password", password)
            page.fill("#totp_code", totp.now())
            page.click(
                "#login-form button[type=submit], "
                "#login-form button:not(#webauthn-login-button)"
            )
            page.wait_for_load_state("networkidle")

            # Dashboard ("the house").
            page.goto(f"{base_url}/ui/", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"dashboard-{scheme}.png"), full_page=True)

            # Tasks.
            page.goto(f"{base_url}/ui/tasks", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"tasks-{scheme}.png"), full_page=True)

            # One apartment.
            page.goto(f"{base_url}/ui/apartments/{APARTMENTS[1].id}", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"apartment-{scheme}.png"), full_page=True)

            # Inventory.
            page.goto(f"{base_url}/ui/inventory", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"inventory-{scheme}.png"), full_page=True)

            # Rollouts.
            page.goto(f"{base_url}/ui/rollouts", wait_until="networkidle")
            page.screenshot(path=str(IMG_DIR / f"rollouts-{scheme}.png"), full_page=True)

            context.close()

        browser.close()


# Needs Pillow, which is only installed ad hoc for this script (pyproject
# .toml's own mypy override comment above says so) -- not a project
# dependency, so not guaranteed present in the test environment. The actual
# per-file path decision it relies on is `_webp_path_for`, tested on its own.
def optimize_images() -> None:  # pragma: no cover
    """Re-saves every captured PNG optimized, and adds a WebP copy next to it."""

    from PIL import Image

    for png_path in sorted(IMG_DIR.glob("*.png")):
        with Image.open(png_path) as img:
            img.save(png_path, optimize=True)
            img.convert("RGB").save(_webp_path_for(png_path), quality=82, method=6)


# Orchestrates seed() -> create_ui_user() -> start_server() -> capture() ->
# optimize_images() against a real throwaway temp directory and a real
# server/browser -- each of those pieces is tested on its own; this glue
# function is exactly what the module docstring and `if __name__ ==
# "__main__"` guard below already exclude for the same reason.
def main() -> int:  # pragma: no cover
    import base64
    import secrets as _secrets

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
    raise SystemExit(main())

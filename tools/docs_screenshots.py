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
from typing import Any, Literal

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
#
# UI-redesign stage 2 (docs/ui-redesign-ia.md): extended from the original
# three (one per floor) so the "Übersicht" site map has a floor with more
# than one apartment side by side (2. OG: WE3 + WE6), and so the action
# inbox shows several *different* kinds of row at once (fault, absence
# alarm, never-reported, battery-low, outdated version) rather than only
# the fault/alarm pair the original three apartments produced.
APARTMENTS = [
    DemoApartment(
        id="musterstr1-we3",
        label="Musterstraße 1, WE 3",
        floor="2. OG",
        orientation="Süd",
        heating_circuits=4,
        state="occupied",
    ),
    DemoApartment(
        id="musterstr1-we6",
        label="Musterstraße 1, WE 6",
        floor="2. OG",
        orientation="Nord",
        heating_circuits=2,
        state="occupied",
    ),
    DemoApartment(
        id="musterstr1-we5",
        label="Musterstraße 1, WE 5",
        floor="3. OG",
        orientation="West",
        heating_circuits=3,
        state="occupied",
    ),
    DemoApartment(
        id="beispielweg9-we1",
        label="Beispielweg 9, WE 1",
        floor="EG",
        orientation="Ost",
        heating_circuits=5,
        state="occupied",
    ),
    DemoApartment(
        id="beispielweg9-we2",
        label="Beispielweg 9, WE 2",
        floor="EG",
        orientation="West",
        heating_circuits=3,
        state="occupied",
    ),
]

# Indices into `APARTMENTS` used by name below, so a reorder of the list
# above cannot silently point `seed()` at the wrong apartment.
_WE3, _WE6, _WE5, _BW1, _BW2 = range(5)


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
        open_faults: list[OpenFault],
        weakest_battery: int,
        worst_signal: int,
        protocol_version: int = PROTOCOL_VERSION,
    ) -> Heartbeat:
        return Heartbeat(
            apartment="placeholder",
            protocol_version=protocol_version,
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

    # WE3: healthy, recent heartbeat -- the plain "in Ordnung" case, and
    # one of two apartments sharing "2. OG" (the site map's floor with
    # more than one unit side by side).
    storage.save_heartbeat(
        APARTMENTS[_WE3].id,
        heartbeat([], weakest_battery=78, worst_signal=61),
        received_at=now,
    )

    # WE6: weak battery (under the 20% threshold) -- shares "2. OG" with
    # WE3, and is this fleet's one "Batterie schwach" inbox item.
    storage.save_heartbeat(
        APARTMENTS[_WE6].id,
        heartbeat([], weakest_battery=15, worst_signal=52),
        received_at=now,
    )

    # WE5: one open fault (sensor fault in the bathroom).
    storage.save_heartbeat(
        APARTMENTS[_WE5].id,
        heartbeat(
            [OpenFault(kind=FaultKind.SENSOR_FAULT, since=now - timedelta(hours=5), zone="Bad")],
            weakest_battery=22,
            worst_signal=38,
        ),
        received_at=now,
    )

    # Beispielweg 9, WE1: last heartbeat old enough to trigger the "not
    # reporting" alarm -- shares "EG" with WE2 below.
    storage.save_heartbeat(
        APARTMENTS[_BW1].id,
        heartbeat([], weakest_battery=55, worst_signal=70, protocol_version=PROTOCOL_VERSION - 1),
        received_at=now - timedelta(minutes=20),
    )
    storage.raise_alarm(
        APARTMENTS[_BW1].id,
        kind="not_reporting",
        urgency="high",
        raised_at=now - timedelta(minutes=14),
    )

    # Beispielweg 9, WE2: never reported at all (no heartbeat saved) --
    # this fleet's "noch nie gemeldet" case, and the second apartment on
    # "EG".

    # A backup for WE3.
    storage.create_backup_record(
        APARTMENTS[_WE3].id,
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
    for entry in (APARTMENTS[_WE3], APARTMENTS[_WE6]):
        storage.create_desired_state_revision(
            entry.id,
            DesiredState(revision=0, services=services, window=window),
            ui_username="demo",
            reason="Demo-Ausgangszustand für die Dokumentations-Screenshots.",
            now=now,
        )

    # A rollout across WE3 (pilot) and WE6 -- stopped after WE3 fails, so
    # the "Übersicht" action inbox also shows a "Rollout wartet auf
    # Entscheidung" row (UI-redesign stage 2's `fleet.ui_overview`).
    rollout = storage.create_rollout(
        service="thermoctl",
        version="0.9.6",
        digest="sha256:" + "cd" * 32,
        apartment_ids=[APARTMENTS[_WE3].id, APARTMENTS[_WE6].id],
        stagger_hours=2.0,
        timeout_hours=1.0,
        ui_username="demo",
        reason="Demo-Rollout für die Dokumentations-Screenshots.",
        now=now,
    )
    storage.start_rollout_apartment(rollout.id, APARTMENTS[_WE3].id, revision=1, now=now)
    storage.mark_rollout_apartment_failed(
        rollout.id, APARTMENTS[_WE3].id, reason="agent rejected", now=now
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


# Pages captured for `site/` at desktop width, by `(path, filename stem)`
# -- the rebuilt information architecture's four areas plus the three
# apartment tabs (docs/ui-redesign-ia.md). Named after the new structure
# throughout; no page keeps an old name ("dashboard", "tasks", "apartment",
# "inventory", "rollouts") since the areas themselves were renamed, not
# just restyled.
_SITE_PAGES: tuple[tuple[str, str], ...] = (
    ("/ui/", "uebersicht"),
    ("/ui/apartments", "wohnungen"),
    (f"/ui/apartments/{APARTMENTS[_WE5].id}?ansicht=ueberblick", "wohnung-ueberblick"),
    (f"/ui/apartments/{APARTMENTS[_WE5].id}?ansicht=wartung", "wohnung-wartung"),
    (f"/ui/apartments/{APARTMENTS[_WE5].id}?ansicht=technik", "wohnung-technik"),
    ("/ui/inventory", "einrichtung"),
    ("/ui/rollouts", "updates"),
)

# A couple of representative mobile shots, bottom tab bar included -- not
# every page (the brief asks for "1-2 mobile shots", not a full mobile
# set): the one page every visit starts on, and the apartment page with
# its tab nav, which is the one other place the responsive layout changes
# shape materially (sidebar -> bottom bar, two columns -> one).
_SITE_MOBILE_PAGES: tuple[tuple[str, str], ...] = (
    ("/ui/", "uebersicht-mobil"),
    (f"/ui/apartments/{APARTMENTS[_WE5].id}?ansicht=ueberblick", "wohnung-ueberblick-mobil"),
)

_SITE_MOBILE_WIDTH = 390


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

            for path, stem in _SITE_PAGES:
                page.goto(f"{base_url}{path}", wait_until="networkidle")
                page.screenshot(path=str(IMG_DIR / f"{stem}-{scheme}.png"), full_page=True)

            context.close()

            # Mobile shots -- own context/login (a fresh TOTP step is
            # needed either way between captures), own narrow viewport, the
            # same `_shoot_full_page` device `capture_ui_redesign_ia` uses
            # below to avoid `full_page=True` double-painting the fixed
            # bottom tab bar (see that function's own docstring).
            _wait_for_fresh_totp_window()
            mobile_context = browser.new_context(
                viewport={"width": _SITE_MOBILE_WIDTH, "height": 900},
                color_scheme=scheme,
            )
            mobile_page = mobile_context.new_page()
            mobile_page.goto(f"{base_url}/ui/login", wait_until="networkidle")
            mobile_page.fill("#username", username)
            mobile_page.fill("#password", password)
            mobile_page.fill("#totp_code", totp.now())
            mobile_page.click(
                "#login-form button[type=submit], "
                "#login-form button:not(#webauthn-login-button)"
            )
            mobile_page.wait_for_load_state("networkidle")

            for path, stem in _SITE_MOBILE_PAGES:
                mobile_page.goto(f"{base_url}{path}", wait_until="networkidle")
                _shoot_full_page(
                    mobile_page, IMG_DIR / f"{stem}-{scheme}.png", _SITE_MOBILE_WIDTH
                )

            mobile_context.close()

        browser.close()


# "Wohnung" tabs captured for docs/ui-redesign/ below, by their own
# `?ansicht=` value and the German-sentence-case filename stem the brief's
# IA uses for them.
_APARTMENT_TABS: tuple[tuple[str, str], ...] = (
    ("ueberblick", "wohnung-ueberblick"),
    ("wartung", "wohnung-wartung"),
    ("technik", "wohnung-technik"),
)

# `(path, filename stem)` for every other page of the rebuilt IA
# (docs/ui-redesign-ia.md) -- captured at both viewport widths, both
# colour schemes, by `capture_ui_redesign_ia` below.
_IA_PAGES: tuple[tuple[str, str], ...] = (
    ("/ui/", "uebersicht"),
    ("/ui/apartments", "wohnungen"),
    ("/ui/inventory", "einrichtung"),
    ("/ui/rollouts", "updates"),
    # Forms pass (UI-redesign stage 2 polish, "die Formulare richtig
    # formatieren"): the owner explicitly asked for at least one
    # screenshot of each major form -- "Konto" (passkey management) and
    # "Neuer Rollout" each live on their own page, unlike the acknowledge-
    # fault/restore/commands forms, which are already inline on a page
    # captured above (wohnung-ueberblick/-wartung).
    ("/ui/account/webauthn", "konto"),
    ("/ui/rollouts/new", "rollout-neu"),
)

_IA_VIEWPORTS: tuple[tuple[int, str], ...] = ((1440, "1440"), (390, "390"))


# Drives a real browser (Playwright/Chromium) against a real, already
# running fleet server, capturing every page of the rebuilt information
# architecture (docs/ui-redesign-ia.md) at both widths the brief names
# (1440px desktop, 390px mobile) and both colour schemes, into
# `docs/ui-redesign/` -- separate from `capture()` above (which feeds
# `site/`'s own, differently-named screenshots) because the two serve
# different documents with a different page set and naming convention.
# Not unit-tested, same reasoning as `capture` -- needs a real browser.
def _shoot_full_page(page: Any, path: Path, width: int) -> None:  # pragma: no cover
    """A full-page screenshot that resizes the viewport to the page's own
    full scrolled height first, then captures without Playwright's own
    `full_page=True` scroll-and-stitch.

    **Round-1 screenshot critique (docs/ui-redesign-ia.md self-review):**
    `full_page=True` against a page with a `position: fixed` element (this
    application's own mobile bottom tab bar, `.app-bottom-nav`, below the
    760px breakpoint) re-paints that fixed element at every scrolled
    "page" Playwright stitches together, so it appeared duplicated,
    floating mid-content, in every captured mobile screenshot -- a
    screenshot-tooling artifact, not a bug in the actual rendered page (a
    real browser only ever paints a `position: fixed` element once, pinned
    to the live viewport). Resizing the viewport to the full content
    height first means there is nothing left to scroll, so there is
    nothing left to stitch.
    """

    # Reset to the base viewport height *before* measuring -- this
    # application's `.app-shell` is `min-height: 100vh` (round-3 screenshot
    # critique caught the bug a first version of this function had: measuring
    # `scrollHeight` while the viewport was still sized from a *previous*
    # page's resize let each page's `100vh` compound into the next,
    # ballooning every subsequent screenshot with growing empty space).
    page.set_viewport_size({"width": width, "height": 900})
    height = page.evaluate("document.documentElement.scrollHeight")
    page.set_viewport_size({"width": width, "height": max(int(height), 900)})
    page.screenshot(path=str(path))


def capture_ui_redesign_ia(  # pragma: no cover
    base_url: str, username: str, password: str, totp_secret: str
) -> None:
    import pyotp
    from playwright.sync_api import sync_playwright

    out_dir = REPO_ROOT / "docs" / "ui-redesign"
    out_dir.mkdir(parents=True, exist_ok=True)

    totp = pyotp.TOTP(totp_secret)
    schemes: tuple[Literal["light", "dark"], ...] = ("light", "dark")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        step = 0
        for width, width_label in _IA_VIEWPORTS:
            for scheme in schemes:
                if step > 0:
                    _wait_for_fresh_totp_window()
                step += 1

                context = browser.new_context(
                    viewport={"width": width, "height": 900}, color_scheme=scheme
                )
                page = context.new_page()

                page.goto(f"{base_url}/ui/login", wait_until="networkidle")
                page.screenshot(path=str(out_dir / f"login-{width_label}-{scheme}.png"))
                page.fill("#username", username)
                page.fill("#password", password)
                page.fill("#totp_code", totp.now())
                page.click(
                    "#login-form button[type=submit], "
                    "#login-form button:not(#webauthn-login-button)"
                )
                page.wait_for_load_state("networkidle")

                for path, stem in _IA_PAGES:
                    page.goto(f"{base_url}{path}", wait_until="networkidle")
                    _shoot_full_page(
                        page, out_dir / f"{stem}-{width_label}-{scheme}.png", width
                    )

                for ansicht, stem in _APARTMENT_TABS:
                    page.goto(
                        f"{base_url}/ui/apartments/{APARTMENTS[_WE5].id}?ansicht={ansicht}",
                        wait_until="networkidle",
                    )
                    _shoot_full_page(
                        page, out_dir / f"{stem}-{width_label}-{scheme}.png", width
                    )

                # One more major form: the stopped demo rollout's own
                # detail page (seed()'s thermoctl rollout across WE3/WE6,
                # stopped after WE3 fails) carries the "Fortsetzen"/
                # "Abbrechen" forms -- reached by following the "Updates"
                # list's own link rather than hard-coding the rollout id
                # (it is a generated uuid, not something this script
                # controls).
                page.goto(f"{base_url}/ui/rollouts", wait_until="networkidle")
                page.click("table a[href^='/ui/rollouts/']")
                page.wait_for_load_state("networkidle")
                _shoot_full_page(
                    page, out_dir / f"rollout-entscheiden-{width_label}-{scheme}.png", width
                )

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
def main() -> int:
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

"""`tools/docs_screenshots.py` (was 0% covered -- see that module's own
docstring). Three kinds of tests:

- `seed()` against a real, migrated temporary SQLite database, read back
  through the real `fleet.storage`/UI view-builder API -- the part that
  actually breaks when either changes shape (same pattern
  `tests/test_storage_rollouts.py`/`tests/test_fleet_desired_state.py`
  already use: `tmp_path`, `fleet.storage.upgrade`, `fleet.storage
  .create_storage`).
- `main()`'s own orchestration, exercised for real with every function it
  calls (`seed`, `create_ui_user`, `_free_port`, `start_server`,
  `_wait_for_server`, `capture`, `optimize_images`) monkeypatched to a
  recorder -- asserting call order, arguments, and that the server
  subprocess is always shut down (terminated normally, or killed if
  `wait()` times out, including when `capture` itself raises).
  `optimize_images()` gets its own, separate real test against an actual
  tiny PNG (`pytest.importorskip("PIL")` -- Pillow is installed ad hoc for
  this script, not a project dependency, so that test skips where it is
  absent instead of mocking it).
- The module's remaining pure helpers (`_free_port`, `_wait_for_server`,
  `_parse_totp_secret`, `_seconds_until_next_totp_step`,
  `_wait_for_fresh_totp_window`, `_webp_path_for`) and the real
  `create_ui_user` subprocess call against `python -m fleet.admin
  create-user`.

Only `capture` and `start_server` are not exercised here -- see their own
`# pragma: no cover` comments in `tools/docs_screenshots.py` for why (a
real browser, a real long-running `uvicorn` server subprocess).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import tools.docs_screenshots as docs_screenshots
from fleet.storage import Storage, create_storage, upgrade
from protocol.heartbeat import FaultKind
from tools.docs_screenshots import (
    APARTMENTS,
    _free_port,
    _parse_totp_secret,
    _seconds_until_next_totp_step,
    _wait_for_fresh_totp_window,
    _wait_for_server,
    _webp_path_for,
    create_ui_user,
    seed,
)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    url = f"sqlite:///{tmp_path}/docs-screenshots-test.db"
    upgrade(url)
    return create_storage(url)


# --------------------------------------------------------------------------
# seed() -- the valuable part: real storage, real UI view builders.
# --------------------------------------------------------------------------


def test_seed_creates_the_fictional_property_and_apartments(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    properties = storage.list_properties()
    assert len(properties) == 1
    assert properties[0].name == "Musterstraße 1 & Beispielweg 9"

    apartments = storage.list_apartments()
    assert {apartment.id for apartment in apartments} == {entry.id for entry in APARTMENTS}
    for entry in APARTMENTS:
        record = storage.get_apartment(entry.id)
        assert record is not None
        assert record.label == entry.label
        assert record.property_id == properties[0].id
        assert record.pilot_mode is False


def test_seed_apartment_1_has_a_healthy_heartbeat_with_no_open_faults(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    latest = storage.get_latest_heartbeat(APARTMENTS[0].id)
    assert latest is not None
    assert latest.heartbeat.open_faults == []
    assert latest.heartbeat.thermoctl.reachable is True
    assert latest.heartbeat.devices.weakest_battery_percent == 78


def test_seed_apartment_2_has_one_open_sensor_fault(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    latest = storage.get_latest_heartbeat(APARTMENTS[1].id)
    assert latest is not None
    assert len(latest.heartbeat.open_faults) == 1
    fault = latest.heartbeat.open_faults[0]
    assert fault.kind == FaultKind.SENSOR_FAULT
    assert fault.zone == "Bad"


def test_seed_apartment_3_has_an_open_not_reporting_alarm(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    alarms = storage.list_alarms_for_apartment(APARTMENTS[2].id)
    assert len(alarms) == 1
    assert alarms[0].kind == "not_reporting"
    assert alarms[0].urgency == "high"
    assert alarms[0].cleared_at is None

    # The house overview categorizes apartment 3 as "in trouble" (an open
    # alarm) -- the one behaviour the demo data exists to show off.
    overview_by_id = {o.apartment_id: o for o in storage.get_house_overview()}
    assert overview_by_id[APARTMENTS[2].id].open_alarm is not None


def test_seed_apartment_1_has_an_operational_data_backup(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    backups = storage.list_backups_for_apartment(APARTMENTS[0].id)
    assert len(backups) == 1
    assert backups[0].kind == "operational_data"
    assert backups[0].size_bytes == 4_194_304
    assert backups[0].content_hash == "sha256:" + "ab" * 32


def test_seed_apartments_1_and_2_have_a_desired_state_revision(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    for entry in APARTMENTS[:2]:
        record = storage.get_desired_state(entry.id)
        assert record is not None
        assert record.revision == 1
        assert record.created_by == "demo"
        history = storage.desired_state_history(entry.id)
        assert len(history) == 1

    # The third apartment deliberately gets no desired state.
    assert storage.get_desired_state(APARTMENTS[2].id) is None


def test_seed_creates_a_rollout_across_apartments_1_and_2(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    rollouts = storage.list_rollouts()
    assert len(rollouts) == 1
    rollout = rollouts[0]
    assert rollout.service == "thermoctl"
    assert rollout.version == "0.9.6"
    assert rollout.digest == "sha256:" + "cd" * 32

    rollout_apartment_ids = {a.apartment_id for a in storage.rollout_apartments(rollout.id)}
    assert rollout_apartment_ids == {APARTMENTS[0].id, APARTMENTS[1].id}


def test_seed_is_readable_through_the_house_overview_ui_builder(tmp_path: Path) -> None:
    """The actual thing the screenshots show -- `fleet.ui_house
    .build_house_overview`'s German-rendered tiles, not just raw storage
    rows."""

    from fleet.ui_house import build_house_overview

    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    tiles = build_house_overview(storage, datetime.now(UTC) + timedelta(minutes=1))
    tiles_by_id = {tile.apartment_id: tile for tile in tiles}
    assert set(tiles_by_id) == {entry.id for entry in APARTMENTS}

    assert tiles_by_id[APARTMENTS[1].id].open_faults != []
    assert tiles_by_id[APARTMENTS[2].id].alarm_since_text is not None


def test_seed_is_readable_through_the_apartment_detail_ui_builder(tmp_path: Path) -> None:
    from fleet.ui_apartment import build_apartment_detail

    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    now = datetime.now(UTC) + timedelta(minutes=1)
    detail = build_apartment_detail(storage, APARTMENTS[1].id, now, days=None)
    assert detail is not None
    assert detail.label == APARTMENTS[1].label
    assert len(detail.open_faults) == 1
    assert detail.desired_state is not None
    assert detail.desired_state.revision == 1

    # Unknown apartment id -> None (same contract `fleet/ui_routes.py`
    # relies on to turn this into a 404).
    assert build_apartment_detail(storage, "no-such-apartment", now, days=None) is None


def test_seed_is_readable_through_the_rollout_ui_builders(tmp_path: Path) -> None:
    from fleet.ui_rollout import build_rollout_detail, build_rollout_list

    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    storage = create_storage(url)

    entries = build_rollout_list(storage)
    assert len(entries) == 1
    assert entries[0].version == "0.9.6"
    assert entries[0].total_apartments == 2

    detail = build_rollout_detail(storage, entries[0].rollout_id)
    assert detail is not None
    assert detail.service == "thermoctl"
    assert {a.apartment_id for a in detail.apartments} == {APARTMENTS[0].id, APARTMENTS[1].id}


def test_seed_is_idempotent_in_shape_but_not_rerunnable_on_the_same_db(tmp_path: Path) -> None:
    """`seed()` is meant to run exactly once per throwaway database (`main`
    creates a fresh temp file every time) -- running it a second time
    against the same, already-seeded database must fail loudly rather than
    silently duplicate the demo property, since `create_apartment` refuses
    a duplicate id."""

    url = f"sqlite:///{tmp_path}/seed-test.db"
    seed(url)
    with pytest.raises(Exception):  # noqa: B017, PT011 -- storage-layer duplicate-id error
        seed(url)


# --------------------------------------------------------------------------
# Pure helpers.
# --------------------------------------------------------------------------


def test_free_port_returns_a_bindable_int() -> None:
    # Not "still free after being released" -- releasing it and rebinding
    # it in a second `socket()` call is an inherent (if narrow) race
    # against anything else on the machine; `_free_port`'s own contract is
    # only ever "an OS-assigned port, free at the moment it was read",
    # which binding *within* `_free_port` itself already proves.
    port = _free_port()
    assert isinstance(port, int)
    assert 0 < port < 65536


def test_wait_for_server_raises_when_nothing_is_listening() -> None:
    port = _free_port()
    with pytest.raises(RuntimeError, match="did not come up in time"):
        _wait_for_server(f"http://127.0.0.1:{port}/healthz", timeout_s=0.3)


def test_wait_for_server_returns_once_a_server_answers() -> None:
    import threading

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- http.server's own method name
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # Keep test output quiet.

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        _wait_for_server(f"http://127.0.0.1:{port}/healthz", timeout_s=5.0)
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            "User 'demo' created.\n"
            "Add this account to an authenticator app (shown once):\n"
            "otpauth://totp/thermoctl-fleet:demo?secret=ABCDEFGH&issuer=thermoctl-fleet\n",
            "ABCDEFGH",
        ),
        (
            "otpauth://totp/x:y?issuer=thermoctl-fleet&secret=ZZZZ9999\n",
            "ZZZZ9999",
        ),
    ],
)
def test_parse_totp_secret_extracts_the_secret(stdout: str, expected: str) -> None:
    assert _parse_totp_secret(stdout) == expected


def test_parse_totp_secret_raises_when_no_secret_is_present() -> None:
    with pytest.raises(RuntimeError, match="Could not find TOTP secret"):
        _parse_totp_secret("User 'demo' already exists.\n")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (0.0, 31.0),
        (1.0, 30.0),
        (29.0, 2.0),
        (29.9, 2.0),
        (30.0, 31.0),
        (59.0, 2.0),
        (60.0, 31.0),
    ],
)
def test_seconds_until_next_totp_step(now: float, expected: float) -> None:
    assert _seconds_until_next_totp_step(now) == expected


def test_wait_for_fresh_totp_window_sleeps_until_the_next_step_boundary() -> None:
    sleeps: list[float] = []
    _wait_for_fresh_totp_window(clock=lambda: 10.0, sleep=sleeps.append)
    assert sleeps == [_seconds_until_next_totp_step(10.0)]


def test_wait_for_fresh_totp_window_uses_real_time_by_default(monkeypatch: object) -> None:
    import time as time_module

    import tools.docs_screenshots as docs_screenshots

    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", sleeps.append)  # type: ignore[attr-defined]
    monkeypatch.setattr(time_module, "time", lambda: 12.0)  # type: ignore[attr-defined]
    docs_screenshots._wait_for_fresh_totp_window()
    assert sleeps == [_seconds_until_next_totp_step(12.0)]


@pytest.mark.parametrize(
    ("png_path", "expected"),
    [
        (Path("site/assets/img/login-light.png"), Path("site/assets/img/login-light.webp")),
        (Path("dashboard-dark.png"), Path("dashboard-dark.webp")),
    ],
)
def test_webp_path_for(png_path: Path, expected: Path) -> None:
    assert _webp_path_for(png_path) == expected


# --------------------------------------------------------------------------
# create_ui_user() -- real `python -m fleet.admin create-user` subprocess.
# --------------------------------------------------------------------------


def test_create_ui_user_runs_the_real_admin_cli_and_returns_a_working_totp_secret(
    tmp_path: Path, monkeypatch: object
) -> None:
    import base64
    import secrets as _secrets

    import pyotp

    from fleet.totp_crypto import decrypt_totp_secret

    totp_key = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    url = f"sqlite:///{tmp_path}/create-ui-user-test.db"
    upgrade(url)

    monkeypatch.setenv("FLEET_TOTP_KEY", totp_key)  # type: ignore[attr-defined]
    secret = create_ui_user(url, totp_key, "demo", "demo-password-not-real-123")

    assert secret
    # Usable as a real TOTP secret.
    pyotp.TOTP(secret).now()

    storage = create_storage(url)
    user = storage.get_ui_user_by_username("demo")
    assert user is not None
    key_bytes = base64.b64decode(totp_key)
    assert decrypt_totp_secret(user.totp_secret, user.id, key_bytes) == secret


def test_create_ui_user_raises_when_the_cli_fails(tmp_path: Path, monkeypatch: object) -> None:
    import base64
    import secrets as _secrets

    totp_key = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    url = f"sqlite:///{tmp_path}/create-ui-user-fail-test.db"
    upgrade(url)
    monkeypatch.setenv("FLEET_TOTP_KEY", totp_key)  # type: ignore[attr-defined]

    # First call succeeds; the second, for the same username, must fail
    # (duplicate) and surface as a RuntimeError, not a silent/garbled
    # secret.
    create_ui_user(url, totp_key, "demo", "demo-password-not-real-123")
    with pytest.raises(RuntimeError, match="create-user failed"):
        create_ui_user(url, totp_key, "demo", "demo-password-not-real-123")


@pytest.mark.skipif(
    sys.platform == "win32", reason="relies on POSIX-style argv, matches the rest of this suite"
)
def test_create_ui_user_passes_argv_with_no_shell(tmp_path: Path, monkeypatch: object) -> None:
    """Defends the `# noqa: S603` in `create_ui_user`: the username is
    passed as one argv element, never shell-interpolated -- a username
    containing a shell metacharacter must be rejected by the CLI itself (or
    accepted literally), never break out of the argument list."""

    import base64
    import secrets as _secrets

    totp_key = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    url = f"sqlite:///{tmp_path}/create-ui-user-argv-test.db"
    upgrade(url)
    monkeypatch.setenv("FLEET_TOTP_KEY", totp_key)  # type: ignore[attr-defined]

    # A username with a shell metacharacter is passed through literally --
    # if this were shell-interpolated, the "; touch ..." part would create
    # a file in `cwd` (the repository root) instead of becoming part of
    # the username.
    marker = tmp_path / "shell-injection-marker"
    hostile = f"demo; touch {marker}"
    # Whether `fleet.admin` accepts or rejects this literal username is not
    # the point here (that is `fleet.admin`'s own business) -- the point is
    # that the `; touch ...` part never ran as a shell command.
    try:
        create_ui_user(url, totp_key, hostile, "demo-password-not-real-123")
    except RuntimeError:
        pass
    assert not marker.exists()


def test_create_ui_user_env_includes_the_given_database_url_and_totp_key(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The subprocess must actually see `FLEET_DATABASE_URL`/`FLEET_TOTP_KEY`
    from its arguments, not leftovers from the parent's own environment --
    proven here by using a *different* stray value in the parent env and
    checking the user still lands in the database named by the argument."""

    import base64
    import secrets as _secrets

    totp_key = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    url = f"sqlite:///{tmp_path}/create-ui-user-env-test.db"
    upgrade(url)

    other_url = f"sqlite:///{tmp_path}/unrelated-stray-env.db"
    monkeypatch.setenv("FLEET_DATABASE_URL", other_url)  # type: ignore[attr-defined]
    monkeypatch.setenv("FLEET_TOTP_KEY", totp_key)  # type: ignore[attr-defined]

    create_ui_user(url, totp_key, "demo", "demo-password-not-real-123")

    storage = create_storage(url)
    assert storage.get_ui_user_by_username("demo") is not None
    assert not Path(other_url.removeprefix("sqlite:///")).exists()


def test_apartments_demo_data_has_fictional_addresses_only() -> None:
    """Guards the module docstring's own claim ("every apartment id,
    address and reading below is fictional") -- a cheap, static check that
    a future edit cannot accidentally slip a real address past."""

    for entry in APARTMENTS:
        assert "Musterstraße" in entry.label or "Beispielweg" in entry.label


# --------------------------------------------------------------------------
# optimize_images() -- a real (tiny) PNG, skipped if Pillow is absent.
# --------------------------------------------------------------------------


def test_optimize_images_writes_a_webp_next_to_each_png(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Pillow is installed ad hoc for this script (pyproject.toml's own
    mypy override comment says so), not a project dependency -- this test
    skips, rather than mocks, where it is absent."""

    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setattr(docs_screenshots, "IMG_DIR", tmp_path)  # type: ignore[attr-defined]

    png_path = tmp_path / "login-light.png"
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(png_path)

    docs_screenshots.optimize_images()

    webp_path = _webp_path_for(png_path)
    assert webp_path.exists()
    with Image.open(webp_path) as webp_img:
        assert webp_img.size == (4, 4)


def test_optimize_images_does_nothing_for_an_empty_directory(
    tmp_path: Path, monkeypatch: object
) -> None:
    pytest.importorskip("PIL")

    monkeypatch.setattr(docs_screenshots, "IMG_DIR", tmp_path)  # type: ignore[attr-defined]
    docs_screenshots.optimize_images()  # Must not raise.
    assert list(tmp_path.glob("*.webp")) == []


# --------------------------------------------------------------------------
# main() -- every function it calls is itself a module-level name this
# module's own call sites use unqualified, so each is monkeypatched to a
# recorder here; no pragma needed on `main` itself (see its own comment).
# --------------------------------------------------------------------------


class _FakeServerProcess:
    def __init__(self, calls: list[tuple[str, ...]], wait_raises: BaseException | None = None):
        self._calls = calls
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self._calls.append(("terminate",))

    def wait(self, timeout: float | None = None) -> None:
        self._calls.append(("wait", str(timeout)))
        if self._wait_raises is not None:
            raise self._wait_raises

    def kill(self) -> None:
        self.killed = True
        self._calls.append(("kill",))


def _patch_main_collaborators(
    monkeypatch: object,
    calls: list[tuple[str, ...]],
    *,
    capture_raises: BaseException | None = None,
    server_wait_raises: BaseException | None = None,
) -> _FakeServerProcess:
    fake_server = _FakeServerProcess(calls, wait_raises=server_wait_raises)

    def fake_seed(database_url: str) -> None:
        calls.append(("seed", database_url))

    def fake_create_ui_user(database_url: str, totp_key: str, username: str, password: str) -> str:
        # The env var `main` itself sets must already be visible here --
        # proves `main` sets it *before* calling this, not after.
        import os as _os

        assert _os.environ["FLEET_TOTP_KEY"] == totp_key
        calls.append(("create_ui_user", database_url, totp_key, username, password))
        return "FAKE-TOTP-SECRET"

    def fake_free_port() -> int:
        calls.append(("free_port",))
        return 54321

    def fake_start_server(database_url: str, port: int) -> _FakeServerProcess:
        calls.append(("start_server", database_url, str(port)))
        return fake_server

    def fake_wait_for_server(url: str, timeout_s: float = 20.0) -> None:
        calls.append(("wait_for_server", url))

    def fake_capture(base_url: str, username: str, password: str, totp_secret: str) -> None:
        calls.append(("capture", base_url, username, password, totp_secret))
        if capture_raises is not None:
            raise capture_raises

    def fake_optimize_images() -> None:
        calls.append(("optimize_images",))

    monkeypatch.setattr(docs_screenshots, "seed", fake_seed)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        docs_screenshots, "create_ui_user", fake_create_ui_user
    )
    monkeypatch.setattr(docs_screenshots, "_free_port", fake_free_port)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        docs_screenshots, "start_server", fake_start_server
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        docs_screenshots, "_wait_for_server", fake_wait_for_server
    )
    monkeypatch.setattr(docs_screenshots, "capture", fake_capture)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        docs_screenshots, "optimize_images", fake_optimize_images
    )
    return fake_server


def test_main_orchestrates_every_step_in_order_with_consistent_arguments(
    monkeypatch: object,
) -> None:
    # `main()` overwrites `FLEET_TOTP_KEY` directly via `os.environ[...] =`
    # (not `setdefault`), which would otherwise leak into every later test
    # in this session (`tests/conftest.py`'s own session-wide default).
    # `monkeypatch.setenv` here still restores the *pre-test* value at
    # teardown regardless of what `main()` sets it to in between.
    monkeypatch.setenv("FLEET_TOTP_KEY", "placeholder-overwritten-by-main")  # type: ignore[attr-defined]

    calls: list[tuple[str, ...]] = []
    _patch_main_collaborators(monkeypatch, calls)

    result = docs_screenshots.main()

    assert result == 0
    names = [call[0] for call in calls]
    assert names == [
        "seed",
        "create_ui_user",
        "free_port",
        "start_server",
        "wait_for_server",
        "capture",
        "terminate",
        "wait",
        "optimize_images",
    ]

    seed_call, create_call, _free_port_call, start_call, wait_for_server_call, capture_call = (
        calls[0],
        calls[1],
        calls[2],
        calls[3],
        calls[4],
        calls[5],
    )

    database_url = seed_call[1]
    assert database_url.startswith("sqlite:///")
    assert database_url.endswith("/fleet-docs-demo.db")
    # The same database_url is threaded through seed/create_ui_user/start_server.
    assert create_call[1] == database_url
    assert start_call[1] == database_url

    assert create_call[3] == "demo"
    assert create_call[4] == "demo-password-not-real-123"

    assert start_call[2] == "54321"
    assert wait_for_server_call[1] == "http://127.0.0.1:54321/healthz"
    assert capture_call[1:] == (
        "http://127.0.0.1:54321",
        "demo",
        "demo-password-not-real-123",
        "FAKE-TOTP-SECRET",
    )

    wait_call = calls[7]
    assert wait_call[1] == "10"


def test_main_still_shuts_down_the_server_when_capture_fails(monkeypatch: object) -> None:
    monkeypatch.setenv("FLEET_TOTP_KEY", "placeholder-overwritten-by-main")  # type: ignore[attr-defined]

    calls: list[tuple[str, ...]] = []
    fake_server = _patch_main_collaborators(
        monkeypatch, calls, capture_raises=RuntimeError("capture blew up")
    )

    with pytest.raises(RuntimeError, match="capture blew up"):
        docs_screenshots.main()

    assert fake_server.terminated is True
    assert fake_server.killed is False
    # optimize_images is only reached after the `finally` block, which is
    # itself only reached after the exception has already propagated past
    # the `with tempfile.TemporaryDirectory(...)` block -- never called.
    assert ("optimize_images",) not in calls


def test_main_kills_the_server_if_a_clean_wait_times_out(monkeypatch: object) -> None:
    monkeypatch.setenv("FLEET_TOTP_KEY", "placeholder-overwritten-by-main")  # type: ignore[attr-defined]

    calls: list[tuple[str, ...]] = []
    fake_server = _patch_main_collaborators(
        monkeypatch,
        calls,
        server_wait_raises=subprocess.TimeoutExpired(cmd="uvicorn", timeout=10),
    )

    result = docs_screenshots.main()

    assert result == 0
    assert fake_server.terminated is True
    assert fake_server.killed is True
    names = [call[0] for call in calls]
    assert names.index("wait") < names.index("kill") < names.index("optimize_images")

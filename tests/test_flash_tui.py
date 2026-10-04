"""Exercise the real Textual screens with headless pilots and fake disks."""

from __future__ import annotations

import asyncio
import hashlib
import lzma
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from textual.pilot import Pilot
from textual.widgets import Button, Input, ProgressBar, Select, Static

from tools.flash.core import FlashError, RemovableDisk
from tools.flash_tui import (
    DiskScreen,
    FlashApp,
    ImageScreen,
    ResultScreen,
    SettingsScreen,
    SummaryScreen,
    WizardScreen,
    WorkScreen,
    friendly_error,
    image_choices,
    main,
    settings_error_message,
)


def _disk(size: int = 32_000_000_000) -> RemovableDisk:
    return RemovableDisk(
        "/dev/disk4", "/dev/rdisk4", "USB card", size,
        volumes=(("BOOT", 1024),),
    )


def _backend(disks: list[RemovableDisk] | None = None) -> MagicMock:
    backend = MagicMock()
    backend.__name__ = "tools.flash.macos"
    backend.list_removable_disks.return_value = [_disk()] if disks is None else disks
    return backend


def _image(tmp_path: Path, *, checksum: str | None = None) -> Path:
    image = tmp_path / "test.img.xz"
    image.write_bytes(lzma.compress(b"payload"))
    if checksum is not None:
        digest = hashlib.sha256(image.read_bytes()).hexdigest() if checksum == "valid" else "0" * 64
        (tmp_path / "SHA256SUMS").write_text(f"{digest}  test.img.xz\n", encoding="utf-8")
    return image


async def _click(pilot: Pilot[None], selector: str) -> None:
    assert await pilot.click(selector)
    await pilot.pause()


async def _to_settings(app: FlashApp, pilot: Pilot[None], image: Path) -> None:
    assert isinstance(app.screen, ImageScreen)
    app.screen.query_one("#image-path", Input).value = str(image)
    await pilot.pause(0.21)
    await _click(pilot, "#next-image")
    await app.workers.wait_for_complete()
    await pilot.pause()
    assert app.image_path == image
    assert "Bytes" in str(app.screen.query_one("#image-info", Static).content)
    # Textual 8.2.8 ignores clicks during Button's 0.2 s active animation.
    await pilot.pause(0.21)
    await _click(pilot, "#next-image")
    assert isinstance(app.screen, DiskScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()
    assert app.disks["/dev/disk4"].volumes[0][0] == "BOOT"
    app.screen.query_one("#disks", Select).value = "/dev/disk4"
    await _click(pilot, "#next-disk")
    assert isinstance(app.screen, SettingsScreen)


async def _to_summary(app: FlashApp, pilot: Pilot[None], image: Path) -> None:
    await _to_settings(app, pilot, image)
    await _click(pilot, "#next-settings")
    assert "ungültiger Wert" in str(app.screen.query_one("#settings-error", Static).content)
    for field, value in {
        "fleet": "https://fleet.example.invalid",
        "fingerprint": "sha256:" + "a" * 64,
        "code": "PLACEHOLDER",
        "ssid": "Home",
        "password": "short",
    }.items():
        app.screen.query_one(f"#{field}", Input).value = value
    await pilot.pause(0.21)
    await _click(pilot, "#next-settings")
    assert "Passwort" in str(app.screen.query_one("#settings-error", Static).content)
    app.screen.query_one("#password", Input).value = "placeholder-password"
    await pilot.pause(0.21)
    await _click(pilot, "#next-settings")
    assert isinstance(app.screen, SummaryScreen)
    assert "••••" in str(app.screen.query_one("#summary", Static).content)


def test_pilot_all_screens_validation_confirmation_and_dry_run(tmp_path: Path) -> None:
    asyncio.run(_dry_run(tmp_path))


async def _dry_run(tmp_path: Path) -> None:
    image = _image(tmp_path, checksum="valid")
    backend = _backend()
    with patch("tools.flash_tui._backend", return_value=backend):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            await _click(pilot, "#next-image")
            assert "vorhandene" in str(app.screen.query_one("#image-info", Static).content)
            await _to_summary(app, pilot, image)
            await _click(pilot, "#start")
            assert "Kennung" in str(app.screen.query_one("#confirm-error", Static).content)
            await pilot.press("d")
            assert app.dry_run
            assert "PROBELAUF" in str(app.screen.query_one("#summary", Static).content)
            app.screen.query_one("#confirmation", Input).value = "/dev/other"
            await pilot.pause(0.21)
            await _click(pilot, "#start")
            assert isinstance(app.screen, SummaryScreen)
            app.screen.query_one("#confirmation", Input).value = "/dev/disk4"
            await pilot.pause(0.21)
            await _click(pilot, "#start")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            assert "Kein Datenträger beschrieben" in str(
                app.screen.query_one("#result", Static).content
            )
            await pilot.press("d")
            assert app.dry_run  # frozen once work has started
            backend.require_write_access.assert_not_called()
            backend.unmount_disk.assert_not_called()
            backend.flash_image.assert_not_called()
            backend.mount_boot_partition.assert_not_called()
            await _click(pilot, "#quit")


def test_pilot_progress_and_success_without_device_write(tmp_path: Path) -> None:
    asyncio.run(_success(tmp_path))


async def _success(tmp_path: Path) -> None:
    image = _image(tmp_path)
    backend = _backend()

    def fake_execute(*args: object, **kwargs: object) -> None:
        progress = kwargs["progress"]
        assert callable(progress)
        progress("Schreiben", 50, 100)
        progress("Prüfen", 100, 100)

    with (
        patch("tools.flash_tui._backend", return_value=backend),
        patch("tools.flash_tui.execute_flash", side_effect=fake_execute) as execute,
    ):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            await _to_summary(app, pilot, image)
            app.screen.query_one("#confirmation", Input).value = "/dev/disk4"
            await _click(pilot, "#start")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            assert "Boot-Dateien" in str(app.screen.query_one("#result", Static).content)
            assert execute.call_args.kwargs["dry_run"] is False
            backend.flash_image.assert_not_called()


def test_pilot_error_screen(tmp_path: Path) -> None:
    asyncio.run(_error(tmp_path))


async def _error(tmp_path: Path) -> None:
    with (
        patch("tools.flash_tui._backend", return_value=_backend()),
        patch("tools.flash_tui.execute_flash", side_effect=FlashError("verification failed")),
    ):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            await _to_summary(app, pilot, _image(tmp_path))
            app.screen.query_one("#confirmation", Input).value = "/dev/disk4"
            await _click(pilot, "#start")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            assert "Prüfung fehlgeschlagen" in str(
                app.screen.query_one("#result", Static).content
            )


def test_pilot_image_selection_and_rejection(tmp_path: Path) -> None:
    asyncio.run(_image_cases(tmp_path))


async def _image_cases(tmp_path: Path) -> None:
    image = _image(tmp_path, checksum="bad")
    with patch("tools.flash_tui._backend", return_value=_backend()):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            app.screen.query_one("#image-path", Input).value = str(image)
            await _click(pilot, "#next-image")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.image_path is None
            assert "SHA256SUMS FEHLER" in str(app.screen.query_one("#image-info", Static).content)
            (tmp_path / "SHA256SUMS").unlink()
            with patch("tools.flash_tui.checksum_status", side_effect=OSError("unreadable")):
                await pilot.pause(0.21)
                await _click(pilot, "#next-image")
                await app.workers.wait_for_complete()
                await pilot.pause()
            assert "unreadable" in str(app.screen.query_one("#image-info", Static).content)


def test_pilot_disk_validation_refresh_and_errors(tmp_path: Path) -> None:
    asyncio.run(_disk_cases(tmp_path))


async def _disk_cases(tmp_path: Path) -> None:
    backend = _backend([_disk(257_000_000_000)])
    with patch("tools.flash_tui._backend", return_value=backend):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            await _click(pilot, "#next-image")
            assert isinstance(app.screen, ImageScreen)
            await _to_settings_start(app, pilot, _image(tmp_path))
            assert isinstance(app.screen, DiskScreen)
            await _click(pilot, "#next-disk")
            assert "nicht zulässig" in str(app.screen.query_one("#disk-info", Static).content)
            app.screen.query_one("#disks", Select).value = "/dev/disk4"
            await pilot.pause(0.21)
            await _click(pilot, "#next-disk")
            assert "nicht zulässig" in str(app.screen.query_one("#disk-info", Static).content)
            backend.list_removable_disks.side_effect = FlashError("lsblk failed")
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Abfrage fehlgeschlagen" in str(
                app.screen.query_one("#disk-info", Static).content
            )
            backend.list_removable_disks.side_effect = None
            backend.list_removable_disks.return_value = []
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Keine zulässigen" in str(app.screen.query_one("#disk-info", Static).content)


async def _to_settings_start(app: FlashApp, pilot: Pilot[None], image: Path) -> None:
    app.screen.query_one("#image-path", Input).value = str(image)
    await pilot.pause(0.21)
    await _click(pilot, "#next-image")
    await app.workers.wait_for_complete()
    await pilot.pause(0.21)
    await _click(pilot, "#next-image")
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_pilot_select_settings_and_bindings(tmp_path: Path) -> None:
    asyncio.run(_select_settings(tmp_path))


async def _select_settings(tmp_path: Path) -> None:
    image = _image(tmp_path)
    with (
        patch("tools.flash_tui._backend", return_value=_backend()),
        patch("tools.flash_tui.image_choices", return_value=[(str(image), str(image))]),
    ):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.press("d")
            assert app.dry_run
            await pilot.press("d")
            assert not app.dry_run
            await pilot.press("r")  # ignored outside disk screen
            app.screen.query_one("#images", Select).value = str(image)
            await pilot.pause()
            assert app.screen.query_one("#image-path", Input).value == str(image)
            await _to_settings(app, pilot, image)
            app.screen.query_one("#backup", Input).value = "invalid"
            for field, value in {
                "fleet": "https://fleet.example.invalid",
                "fingerprint": "sha256:" + "a" * 64,
                "code": "PLACEHOLDER",
            }.items():
                app.screen.query_one(f"#{field}", Input).value = value
            await _click(pilot, "#next-settings")
            assert "age1" in str(app.screen.query_one("#settings-error", Static).content)
            app.screen.query_one("#backup", Input).value = "age1public"
            app.screen.query_one("#ssid", Input).value = "Home"
            await pilot.pause(0.21)
            await _click(pilot, "#next-settings")
            assert "Passwort" in str(
                app.screen.query_one("#settings-error", Static).content
            )
            app.screen.query_one("#password", Input).value = "placeholder-password"
            await pilot.pause(0.21)
            await _click(pilot, "#next-settings")
            assert isinstance(app.screen, SummaryScreen)
            assert app.settings["backup_recipients"] == ["age1public"]


def test_image_choices_and_messages(tmp_path: Path) -> None:
    image = _image(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    second = _image(downloads)
    with (patch("tools.flash_tui.Path.cwd", return_value=tmp_path),
          patch("tools.flash_tui.Path.home", return_value=tmp_path)):
        expected_paths = sorted([image.resolve(), second.resolve()])
        assert image_choices() == [(str(path), str(path)) for path in expected_paths]
    messages = {
        "root required": "sudo",
        "administratorrechte": "Administrator",
        "identity changed": "geändert",
        "disappeared": "geändert",
        "verification FAILED": "Prüfung",
        "readback short": "Prüfung",
        "SHA256SUMS mismatch": "Prüfsumme",
        "larger than target": "Image",
        "exceeds 256": "Image",
        "lsblk failed": "Abfrage",
        "diskutil failed": "Abfrage",
        "powershell failed": "Abfrage",
        "unknown": "Vorgang",
    }
    for error, expected in messages.items():
        assert expected in friendly_error(RuntimeError(error))
    assert "Passwort" in settings_error_message(ValueError("password invalid"))
    assert "SSID" in settings_error_message(ValueError("ssid invalid"))
    assert "age1" in settings_error_message(ValueError("recipient invalid"))
    assert "Einstellungen" in settings_error_message(ValueError("unknown"))
    with pytest.raises(ValidationError) as exc:
        from protocol.registration import AgentRegistrationFile
        AgentRegistrationFile(fleet_address="", certificate_fingerprint="", registration_code="")
    assert "Fleet-Adresse" in settings_error_message(exc.value)
    assert "Registrierungscode" in settings_error_message(exc.value)
    assert "registration_code" not in settings_error_message(exc.value)


def test_screen_contract_and_entrypoint() -> None:
    with pytest.raises(NotImplementedError):
        list(WizardScreen().fields())
    with patch.object(FlashApp, "run") as run:
        main()
    run.assert_called_once_with()


def test_windows_title_and_result_variants() -> None:
    asyncio.run(_windows_and_results())


async def _windows_and_results() -> None:
    backend = _backend()
    backend.__name__ = "tools.flash.windows"
    with patch("tools.flash_tui._backend", return_value=backend):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            assert "Windows ungetestet" in app.title
            for error, dry_run, expected in (
                (None, False, "Boot-Dateien"),
                (None, True, "Probelauf abgeschlossen"),
                ("Fehlertext", False, "Fehlertext"),
            ):
                app.push_screen(ResultScreen(error, dry_run=dry_run))
                await pilot.pause()
                assert expected in str(app.screen.query_one("#result", Static).content)
            app.action_toggle_dry()
            assert not app.dry_run


def test_pilot_mounted_work_progress_and_other_button_events() -> None:
    asyncio.run(_work_progress_and_events())


async def _work_progress_and_events() -> None:
    with patch("tools.flash_tui._backend", return_value=_backend()):
        app = FlashApp()
        async with app.run_test(size=(100, 34)) as pilot:
            image_screen = app.screen
            assert isinstance(image_screen, ImageScreen)
            image_screen.on_button_pressed(Button.Pressed(Button(id="other")))
            disk_screen = DiskScreen()
            with patch.object(disk_screen, "refresh_disks"):
                app.push_screen(disk_screen)
                await pilot.pause()
            disk_screen.on_button_pressed(Button.Pressed(Button(id="other")))
            settings_screen = SettingsScreen()
            app.push_screen(settings_screen)
            await pilot.pause()
            settings_screen.on_button_pressed(Button.Pressed(Button(id="other")))
            summary_screen = SummaryScreen()
            app.disk = _disk()
            app.settings = {"fleet_address": "fleet", "wifi_ssid": ""}
            app.push_screen(summary_screen)
            await pilot.pause()
            summary_screen.on_button_pressed(Button.Pressed(Button(id="other")))
            work_screen = WorkScreen()
            with patch.object(work_screen, "run_flash"):
                app.push_screen(work_screen)
                await pilot.pause()
            assert isinstance(app.screen, WorkScreen)
            work_screen._progress("Schreiben", 50, 100, 1048576, 5)
            assert "50/100" in str(work_screen.query_one("#work-status", Static).content)
            assert work_screen.query_one("#progress", ProgressBar).progress == 50

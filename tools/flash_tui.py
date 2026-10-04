"""Keyboard-first German Textual image writer for macOS, Linux and Windows."""

from __future__ import annotations

# mypy: disable-error-code="import-not-found,misc,untyped-decorator"
# Textual is an optional extra; the base development environment need not install it.
import lzma
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, Select, Static

from tools.flash.core import (
    FlashError,
    RemovableDisk,
    checksum_status,
    execute_flash,
    validate_settings,
)
from tools.flash_image import _backend


def image_choices() -> list[tuple[str, str]]:
    """Offer compressed images from the current directory and Downloads."""
    found = {
        path.resolve()
        for folder in (Path.cwd(), Path.home() / "Downloads")
        for path in folder.glob("*.img.xz")
        if path.is_file()
    }
    return [(str(path), str(path)) for path in sorted(found)]


def friendly_error(error: Exception) -> str:
    """Give the operator a short next step without echoing credentials."""
    detail = str(error).lower()
    if "root required" in detail:
        return (
            "Schreibrechte fehlen. Starten Sie das Terminal mit sudo und versuchen Sie es erneut."
        )
    if "administratorrechte" in detail:
        return "Administratorrechte fehlen. Starten Sie das Terminal als Administrator."
    if "identity" in detail or "changed" in detail or "disappeared" in detail:
        return (
            "Datenträger wurde geändert oder entfernt. "
            "Karte neu einstecken und Auswahl aktualisieren."
        )
    if "verification" in detail or "readback" in detail:
        return (
            "Prüfung fehlgeschlagen. Verwenden Sie diese Karte nicht; "
            "wählen Sie einen anderen Datenträger."
        )
    if "sha256sums" in detail:
        return "Prüfsumme stimmt nicht. Laden Sie das Image erneut herunter."
    if "larger than target" in detail or "exceeds 256" in detail:
        return "Image oder Datenträgergröße ist nicht zulässig. Wählen Sie eine passende Karte."
    if "lsblk" in detail or "diskutil" in detail or "powershell" in detail:
        return "Datenträger-Abfrage fehlgeschlagen. Werkzeug prüfen und erneut suchen."
    return "Vorgang fehlgeschlagen. Image und Datenträger prüfen und erneut versuchen."


def settings_error_message(error: ValueError | ValidationError) -> str:
    """Render validation errors in German without including entered secrets."""
    if isinstance(error, ValidationError):
        names = {
            "fleet_address": "Fleet-Adresse",
            "certificate_fingerprint": "Zertifikats-Fingerabdruck",
            "registration_code": "Registrierungscode",
        }
        return "; ".join(
            f"{names.get(str(issue['loc'][0]), str(issue['loc'][0]))}: ungültiger Wert"
            for issue in error.errors(include_input=False)
        )
    detail = str(error).lower()
    if "password" in detail or "passwort" in detail:
        return "WLAN-Passwort: 8–63 druckbare ASCII-Zeichen oder 64 Hex-Zeichen eingeben."
    if "ssid" in detail:
        return "WLAN SSID: 1–32 Bytes ohne Steuerzeichen eingeben."
    if "recipient" in detail:
        return "Backup-Empfänger: nur öffentliche age1-Schlüssel eingeben."
    return "Einstellungen prüfen und erneut versuchen."


class WizardScreen(Screen[None]):
    """Common compact screen layout."""

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            yield from self.fields()
        yield Footer()

    def fields(self) -> ComposeResult:
        raise NotImplementedError


class ImageScreen(WizardScreen):
    def fields(self) -> ComposeResult:
        yield Label("1/5 · Image wählen")
        yield Select(image_choices(), prompt="*.img.xz auswählen", id="images")
        yield Input(placeholder="Pfad zur .img.xz", id="image-path")
        yield Static("", id="image-info")
        yield Button("Image prüfen", id="next-image", variant="primary")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "images" and isinstance(event.value, str):
            self.query_one("#image-path", Input).value = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "next-image":
            return
        path = Path(self.query_one("#image-path", Input).value).expanduser()
        if not path.is_file() or not path.name.endswith(".img.xz"):
            self.query_one("#image-info", Static).update("Bitte eine vorhandene .img.xz wählen.")
            return
        app = self.app
        assert isinstance(app, FlashApp)
        if app.image_path == path and app.image_status != "SHA256SUMS FEHLER":
            app.push_screen(DiskScreen())
            return
        self.inspect_image(path)

    @work(thread=True)
    def inspect_image(self, path: Path) -> None:
        try:
            status = checksum_status(path)
            size = path.stat().st_size
            if status == "SHA256SUMS FEHLER":
                self.app.call_from_thread(self._reject_image, f"{size} Bytes · {status}")
                return
            self.app.call_from_thread(self._accept, path, size, status)
        except (OSError, UnicodeError, lzma.LZMAError) as exc:
            self.app.call_from_thread(self._set_image_info, str(exc))

    def _set_image_info(self, message: str) -> None:
        self.query_one("#image-info", Static).update(message)

    def _reject_image(self, message: str) -> None:
        app = self.app
        assert isinstance(app, FlashApp)
        app.image_path = None
        app.image_status = "SHA256SUMS FEHLER"
        self._set_image_info(message)

    def _accept(self, path: Path, size: int, status: str) -> None:
        app = self.app
        assert isinstance(app, FlashApp)
        app.image_path = path
        app.image_size_bytes = size
        app.image_status = status
        self.query_one("#image-info", Static).update(f"{size} Bytes · {status}")
        self.query_one("#next-image", Button).label = "Weiter"


class DiskScreen(WizardScreen):
    def fields(self) -> ComposeResult:
        yield Label("2/5 · Speicherkarte wählen · r = aktualisieren")
        yield Static("Datenträger werden gesucht …", id="disk-info")
        yield Select([], prompt="Datenträger wählen", id="disks")
        yield Button("Weiter", id="next-disk", variant="primary")

    def on_mount(self) -> None:
        self.refresh_disks()

    @work(thread=True)
    def refresh_disks(self) -> None:
        try:
            backend = _backend()
            disks = backend.list_removable_disks()
            self.app.call_from_thread(self._show_disks, disks)
        except (FlashError, OSError) as exc:
            self.app.call_from_thread(self._set_disk_info, friendly_error(exc))

    def _set_disk_info(self, message: str) -> None:
        self.query_one("#disk-info", Static).update(message)

    def _show_disks(self, disks: list[RemovableDisk]) -> None:
        app = self.app
        assert isinstance(app, FlashApp)
        app.disks = {d.device: d for d in disks}
        labels = []
        for disk in disks:
            volumes = ", ".join(v[0] for v in disk.volumes) or "keine Volumes"
            reason = " · über 256 GB: gesperrt" if disk.size_bytes > 256_000_000_000 else ""
            labels.append(
                (
                    f"{disk.device} · {disk.name} · {disk.size_human} · {volumes}{reason}",
                    disk.device,
                )
            )
        self.query_one("#disks", Select).set_options(labels)
        image_note = f"Image: {app.image_size_bytes} Bytes · {app.image_status}. "
        self.query_one("#disk-info", Static).update(
            image_note
            + (
                "Nur externe, physische ganze Datenträger ohne System-/Boot-Volume. "
                "Andere Datenträger sind gesperrt."
                if disks
                else "Keine zulässigen Datenträger gefunden. r = neu suchen."
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "next-disk":
            return
        app = self.app
        assert isinstance(app, FlashApp)
        value = self.query_one("#disks", Select).value
        disk = app.disks.get(value) if isinstance(value, str) else None
        if disk is None or disk.size_bytes > 256_000_000_000:
            self.query_one("#disk-info", Static).update(
                "Datenträger nicht zulässig (maximal 256 GB)."
            )
            return
        app.disk = disk
        app.push_screen(SettingsScreen())


class SettingsScreen(WizardScreen):
    def fields(self) -> ComposeResult:
        yield Label("3/5 · Einstellungen")
        for name, label in (
            ("fleet", "Fleet-Adresse"),
            ("fingerprint", "Zertifikats-Fingerabdruck"),
            ("code", "Registrierungscode"),
            ("backup", "Backup-Empfänger (optional)"),
            ("ssid", "WLAN SSID (optional)"),
            ("password", "WLAN Passwort (optional)"),
        ):
            yield Input(placeholder=label, id=name, password=name in {"code", "password"})
        yield Static("", id="settings-error")
        yield Button("Weiter", id="next-settings", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "next-settings":
            return
        app = self.app
        assert isinstance(app, FlashApp)
        values = {
            name: self.query_one(f"#{name}", Input).value
            for name in ("fleet", "fingerprint", "code", "backup", "ssid", "password")
        }
        recipients = [line.strip() for line in values["backup"].splitlines() if line.strip()]
        try:
            validate_settings(
                values["fleet"],
                values["fingerprint"],
                values["code"],
                recipients,
                values["ssid"],
                values["password"],
            )
        except ValidationError as exc:
            self.query_one("#settings-error", Static).update(settings_error_message(exc))
            return
        except ValueError as exc:
            self.query_one("#settings-error", Static).update(settings_error_message(exc))
            return
        app.settings = dict(
            fleet_address=values["fleet"],
            certificate_fingerprint=values["fingerprint"],
            registration_code=values["code"],
            backup_recipients=recipients,
            wifi_ssid=values["ssid"],
            wifi_password=values["password"],
        )
        app.push_screen(SummaryScreen())


class SummaryScreen(WizardScreen):
    def fields(self) -> ComposeResult:
        yield Label("4/5 · Zusammenfassung + Bestätigung")
        yield Static("", id="summary")
        yield Input(placeholder="Datenträgerkennung exakt eintippen", id="confirmation")
        yield Static("", id="confirm-error")
        yield Button("Schreiben / Probelauf", id="start", variant="warning")

    def on_mount(self) -> None:
        app = self.app
        assert isinstance(app, FlashApp)
        disk = app.disk
        assert disk is not None
        self.query_one("#summary", Static).update(
            f"Image: {app.image_path}\nDatenträger: {disk.device} · {disk.name} · {disk.size_human}"
            f"\nFleet: {app.settings['fleet_address']}\nRegistrierungscode: ••••"
            f"\nWLAN: {app.settings['wifi_ssid'] or 'ohne'}\n"
            f"{'PROBELAUF · kein Schreiben' if app.dry_run else 'ALLE DATEN WERDEN GELÖSCHT'}\n"
            f"Zur Bestätigung exakt {disk.device} eingeben."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "start":
            return
        app = self.app
        assert isinstance(app, FlashApp)
        if app.disk is None or self.query_one("#confirmation", Input).value != app.disk.device:
            self.query_one("#confirm-error", Static).update(
                "Kennung stimmt nicht. Kein Schreibvorgang gestartet."
            )
            return
        app.push_screen(WorkScreen())


class WorkScreen(WizardScreen):
    def fields(self) -> ComposeResult:
        yield Label("5/5 · Schreiben und Prüfen")
        yield Static("Vorbereitung …", id="work-status")
        yield ProgressBar(total=100, id="progress")

    def on_mount(self) -> None:
        self.run_flash()

    @work(thread=True, exclusive=True)
    def run_flash(self) -> None:
        app = self.app
        assert isinstance(app, FlashApp)
        assert app.disk is not None and app.image_path is not None
        started = time.monotonic()

        def progress(stage: str, count: int, total: int) -> None:
            elapsed = max(time.monotonic() - started, 0.001)
            speed = count / elapsed
            eta = (total - count) / speed if speed else 0
            self.app.call_from_thread(self._progress, stage, count, total, speed, eta)

        try:
            execute_flash(
                _backend(),
                app.image_path,
                app.disk,
                dry_run=app.dry_run,
                progress=progress,
                **app.settings,
            )
        except (FlashError, OSError, ValueError, ValidationError, lzma.LZMAError) as exc:
            self.app.call_from_thread(self.app.push_screen, ResultScreen(friendly_error(exc)))
            return
        self.app.call_from_thread(self.app.push_screen, ResultScreen(None, dry_run=app.dry_run))

    def _progress(self, stage: str, count: int, total: int, speed: float, eta: float) -> None:
        self.query_one("#progress", ProgressBar).update(progress=100 * count / max(total, 1))
        self.query_one("#work-status", Static).update(
            f"{stage}: {count}/{total} Bytes · {speed / 1048576:.1f} MiB/s · ETA {eta:.0f} s"
        )


class ResultScreen(WizardScreen):
    def __init__(self, error: str | None, *, dry_run: bool = False) -> None:
        super().__init__()
        self.error = error
        self.dry_run = dry_run

    def fields(self) -> ComposeResult:
        yield Label("Fehler" if self.error else "Fertig")
        yield Static(
            self.error
            or (
                "Probelauf abgeschlossen. Kein Datenträger beschrieben."
                if self.dry_run
                else "Image geprüft und Boot-Dateien geschrieben."
            ),
            id="result",
        )
        yield Static(
            "Datenträger prüfen und erneut versuchen."
            if self.error
            else (
                "Zum Schreiben Probelauf ausschalten und erneut bestätigen."
                if self.dry_run
                else "Datenträger sicher auswerfen und Gerät starten."
            )
        )
        yield Button("Beenden", id="quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.app.exit()


class FlashApp(App[None]):
    """Image writer. Windows support is ungetestet on real hardware."""

    CSS = "#body { padding: 0 1; } Input, Select { width: 100%; }"
    BINDINGS = [
        ("d", "toggle_dry", "Probelauf"),
        ("r", "refresh_disks", "Aktualisieren"),
        ("t", "toggle_dark", "Hell/Dunkel"),
        ("q", "quit", "Beenden"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.image_path: Path | None = None
        self.image_size_bytes = 0
        self.image_status = ""
        self.disk: RemovableDisk | None = None
        self.disks: dict[str, RemovableDisk] = {}
        self.settings: dict[str, Any] = {}
        self.dry_run = False

    def on_mount(self) -> None:
        self.push_screen(ImageScreen())
        if _backend().__name__.endswith("windows"):
            self.title = "thermoctl Flash · Windows ungetestet"
        else:
            self.title = "thermoctl Flash"

    def action_refresh_disks(self) -> None:
        if isinstance(self.screen, DiskScreen):
            self.screen.refresh_disks()

    def action_toggle_dry(self) -> None:
        if isinstance(self.screen, (WorkScreen, ResultScreen)):
            return
        self.dry_run = not self.dry_run
        self.sub_title = "PROBELAUF · keine Schreibzugriffe" if self.dry_run else "Schreibmodus"
        if isinstance(self.screen, SummaryScreen):
            self.screen.on_mount()


def main() -> None:
    FlashApp().run()


if __name__ == "__main__":  # pragma: no cover -- launching the console requires a real terminal
    main()

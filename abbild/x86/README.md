# abbild/x86/ — Debian 13 „Trixie" amd64, minimal (Abschnitt 19.1)

| | |
|---|---|
| Grundlage | Debian 13 „Trixie" **amd64**, minimal |
| Ziel | Mini-PC mit N100, Thin Client, alles, was kein Raspberry Pi ist |
| Architektur | `amd64` -- nur 64-Bit, siehe `abbild/README.md` |
| Start | EFI, Debian-Standardkernel |
| Alles andere | siehe [`../gemeinsam/`](../gemeinsam/) -- Paketliste, udev-Regel, `unattended-upgrades`, Wächter-Einheit |

Dieselbe Grundlage wie `abbild/pi/` (Debian 13), nur ohne Raspberry-Pi-Kernel
und mit EFI-Start statt FAT32-`/boot/firmware`. Die Begründung für "Debian statt
Alpine" steht in `abbild/README.md`.

## Stand dieses Gerüsts

Kein Abbild wird hier gebaut. Vorgesehener Bauweg (Abschnitt 19.4): **`mkosi`**
oder **`debos`** -- beide erzeugen aus einer deklarativen Konfiguration ein
fertiges Debian-Abbild, ohne einen eigenen Installationsvorgang nachzubauen.
Welches der beiden Werkzeuge, ist noch nicht entschieden (siehe
docs/STATUS.md) -- `mkosi` ist enger an systemd angelehnt (passend zum
Wächter), `debos` ist älter und in Debian selbst paketiert.

Fehlt vollständig:

- eine `mkosi.conf`/`debos`-Rezeptdatei, die dieselben gemeinsamen Schritte
  wie `abbild/pi/` ausführt: `abbild/gemeinsam/paketliste.txt` installieren,
  udev-Regel und `unattended-upgrades`-Konfiguration einspielen, die
  Wächter-Einheit aus `../../waechter/thermoctl-waechter.service` kopieren und
  aktivieren, `melder-anmeldung.leer.json` als `melder-anmeldung.json` auf die
  Startpartition legen,
- EFI-Bootpartition und -Bootloader-Konfiguration,
- das Vorladen des Agent-Container-Abbilds,
- Kompression, Prüfsummenbildung, Anbindung an `v*`-Tags -- wie bei `abbild/pi/`.

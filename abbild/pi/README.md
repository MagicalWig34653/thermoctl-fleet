# abbild/pi/ — Raspberry Pi OS Lite 64-Bit (Abschnitt 19.1)

| | |
|---|---|
| Grundlage | Raspberry Pi OS Lite **64-Bit** (Debian 13 „Trixie", Kernel 6.12 LTS) |
| Ziel | Raspberry Pi 4 und 5 |
| Architektur | `arm64` -- nur 64-Bit, siehe `abbild/README.md` |
| Startpartition | FAT32 unter `/boot/firmware` (Raspberry-Pi-eigenes Layout) |
| Alles andere | siehe [`../gemeinsam/`](../gemeinsam/) -- Paketliste, udev-Regel, `unattended-upgrades`, Wächter-Einheit |

Raspberry Pi OS ist Debian; der einzige Unterschied zu `abbild/x86/` ist Kernel
und Firmware von Raspberry Pi sowie die FAT32-Startpartition -- die
Begründung dafür steht in `abbild/README.md`.

## Stand dieses Gerüsts

Kein Abbild wird hier gebaut. Vorgesehener Bauweg (Abschnitt 19.4):
**pi-gen**, das offizielle Raspberry-Pi-Werkzeug, das aus einer
Stufenkonfiguration ein fertiges `.img` erzeugt -- damit lässt sich auf
Raspberry Pi OS Lite als Grundlage aufsetzen, statt ein Abbild von Grund auf
zu bauen.

Fehlt vollständig:

- eine pi-gen-Konfiguration (`config`-Datei plus eigene Stufe), die
  `abbild/gemeinsam/paketliste.txt` installiert, `abbild/gemeinsam/udev/` und
  `abbild/gemeinsam/unattended-upgrades/` einspielt, die Wächter-Einheit aus
  `../../waechter/thermoctl-waechter.service` kopiert und aktiviert, und
  `abbild/gemeinsam/melder-anmeldung.leer.json` als `melder-anmeldung.json`
  auf die Startpartition legt,
- das Vorladen des Agent-Container-Abbilds (Abschnitt 19.3),
- die Kompression und Prüfsummenbildung des fertigen `.img.xz` (Abschnitt 19.4),
- die Anbindung an `v*`-Tags in `.github/workflows/abbild.yml` -- dort bislang
  nur ein Kommentar, kein Baulauf.

Ein vollständiger pi-gen-Lauf dauert je nach Läufer 30-60 Minuten und gehört
laut Auftrag **nicht** in jeden Commit -- `.github/workflows/abbild.yml` prüft
vorerst nur, dass die Konfiguration für einen solchen Lauf plausibel ist.

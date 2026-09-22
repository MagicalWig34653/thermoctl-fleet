# abbild/ — die vorbereiteten Systemabbilder (Abschnitt 19)

**Kein eigenes Betriebssystem.** Ein „thermoctlOS" nach dem Vorbild von Home
Assistant OS hieße eigener Kernel, eigener Bootloader und die Verantwortung für
jede Lücke im Unterbau. Gebaut wird stattdessen ein **Rezept**, das aus einer
fertigen Linux-Ausgabe ein einsatzbereites Gerät für die Basisstation einer
Wohnung macht. Bei einem eigenen System gehören die Sicherheitslücken uns, bei
einem vorbereiteten Abbild gehören sie Debian.

## Zwei Ziele, ein Rezept

| | [`pi/`](pi/) | [`x86/`](x86/) |
|---|---|---|
| Grundlage | Raspberry Pi OS Lite **64-Bit** (Debian 13 „Trixie", Kernel 6.12 LTS) | Debian 13 „Trixie" **amd64**, minimal |
| Für | Raspberry Pi 4 und 5 | Mini-PC mit N100, Thin Client, alles andere |
| Unterschiede | Kernel/Firmware von Raspberry Pi, Startpartition FAT32 unter `/boot/firmware` | Debian-Kernel, EFI-Start |

[`gemeinsam/`](gemeinsam/) enthält alles andere: die Paketliste, die udev-Regel
für den Zigbee-Stick, die `unattended-upgrades`-Konfiguration, die
Protokoll-im-Arbeitsspeicher-Einstellung und die leere
`melder-anmeldung.json`-Vorlage für die Startpartition. Die systemd-Einheit des
Wächters ist **nicht** hier dupliziert -- sie lebt bei ihrem Code unter
[`../waechter/thermoctl-waechter.service`](../waechter/thermoctl-waechter.service)
und wird beim Bau in beide Abbilder kopiert.

**Warum genau diese zwei Ziele und nicht Alpine:** Raspberry Pi OS *ist*
Debian. Ein Rezept, zwei Ziele, ein Wartungsweg -- dieselben Paketnamen,
dieselben systemd-Einheiten, derselbe Wächter ohne zweite Fassung. Alpine
benutzt OpenRC statt systemd (der Wächter bräuchte eine zweite Umsetzung -- die
Doppelpflege, die dieses Repository überall sonst vermeidet), dazu musl statt
glibc (gelegentliche Reibung mit Python-Paketen) und rund zwei statt fünf
Jahre Unterstützung je Zweig. Der Vorteil wäre ein rund 100 MB kleinerer
Abdruck -- bei 2 GB Arbeitsspeicher und SSD keine Währung, in der sich das
rechnet.

**Nur 64-Bit**, in beiden Fällen — schon weil das thermoctl-Abbild selbst nur
für `linux/amd64` und `linux/arm64` gebaut wird (siehe `docker/`).

**Unterstützungsdauer:** Debian 13 „Trixie" volle Unterstützung bis 9. August
2028, danach LTS bis 30. Juni 2030; Raspberry Pi OS folgt seit Oktober 2025
derselben Grundlage. Der Wechsel auf die nächste Debian-Ausgabe ist **keine**
Aktualisierung im laufenden Betrieb, sondern eine Welle neuer Karten/Datenträger
über den Ersatzgeräte-Weg (Abschnitt 15.3) -- eine Wohnung nach der anderen,
Pilotwohnung zuerst.

## Was damit aufgegeben wird

Die A/B-Aktualisierung des Betriebssystems, die Home Assistant OS hat. Ein
misslungenes `apt`-Update ist damit theoretisch ein Vor-Ort-Termin. Dagegen
steht: Sicherheitsaktualisierungen in Debian sind eng begrenzt und brechen
selten, die Anwendung hat ihre eigene A/B-Sicherung über die Digests
(Abschnitt 17), und für den Rest liegt ein vorbereitetes Ersatzgerät im Regal.

## Stand dieses Gerüsts

Kein Abbild wird hier tatsächlich gebaut. Vorhanden sind die Ordnerstruktur,
die dokumentierte Paketliste, die Wiederverwendung der Wächter-Einheit, ein
Bauweg-Entwurf je Variante (`pi/README.md`, `x86/README.md`) und
`.github/workflows/abbild.yml`, das vorerst nur die Konfiguration liest und
die Paketliste validiert -- kein echter `pi-gen`- oder `mkosi`/`debos`-Lauf,
siehe die Begründung in der Workflow-Datei selbst.

Was fehlt: der eigentliche Bau (Abschnitt 19.4), das Vorbereitungswerkzeug für
die Startpartition (Abschnitt 19.5), und die Freigabe-Kopplung an `v*`-Tags
(zwei `.img.xz` samt Prüfsummen, mit derselben Versionsnummer wie die
mitgelieferte Wächter-Fassung).

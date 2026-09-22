# abbild/gemeinsam/ — was beide Abbilder teilen (Abschnitt 19.3)

Alles hier gilt unverändert für `abbild/pi/` und `abbild/x86/`. Der Grund ist
Abschnitt 19.1: Raspberry Pi OS ist Debian, also braucht es keine zweite
Fassung von Paketnamen, Einheiten oder Aktualisierungsregeln -- nur zweimal
denselben Kernschritt "fertiges Debian nehmen, das hier anwenden".

| Datei/Ordner | Zweck |
|---|---|
| `paketliste.txt` | Pakete, die beide Abbilder installieren (Container-Laufzeit, Zeitsynchronisation, Hardware-Watchdog, `unattended-upgrades`, log2ram, udev, WireGuard-Werkzeuge) |
| `udev/99-zigbee-stick.rules` | Fester Gerätename für den Zigbee-Funkstick, damit er nicht mal `ttyUSB0`, nach einem Neustart `ttyUSB1` heißt |
| `unattended-upgrades/` | Sicherheitsaktualisierungen automatisch, Neustart nur im Zeitfenster |
| `melder-anmeldung.leer.json` | Vorlage für die Startpartition (Abschnitt 15.3, 19.5) -- die Felder aus `protokoll.anmeldung.MelderAnmeldedatei`, leer, bis das Vorbereitungswerkzeug sie beim Schreiben des Abbilds füllt |

**Die systemd-Einheit des Wächters liegt bewusst nicht hier**, sondern bei
ihrem Code unter
[`../../waechter/thermoctl-waechter.service`](../../waechter/thermoctl-waechter.service).
Eine Kopie an zwei Stellen im selben Repository wäre genau die Art von
Doppelpflege, die dieses Repository überall sonst vermeidet (siehe
`README.md`, "Ein Repository, zwei Abbilder"); der Bauschritt in `pi/` und
`x86/` kopiert sie stattdessen von dort ins Abbild.

## Was laut Abschnitt 19.3 sonst noch in beide Abbilder gehört

Noch nicht als Datei hier angelegt, weil es keine reine Konfigurationsdatei
ist, sondern einen echten Bauschritt braucht (siehe `docs/STATUS.md`):

- Container-Laufzeit und Hardware-Watchdog **eingeschaltet** (nicht nur
  installiert).
- Agent-Abbild bereits vorgeladen (`docker pull`/`docker load` während des
  Baus, nicht zur Laufzeit).
- WireGuard **installiert, aber nicht eingerichtet** (Abschnitt 14).
- Kein SSH-Passwortzugang; Schlüssel werden beim Vorbereiten hinterlegt oder
  gar nicht (Abschnitt 19.3).

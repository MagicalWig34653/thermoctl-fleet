# waechter — der Prozess, der den Agenten tauscht (Abschnitt 17, 18.3)

**In Go geschrieben, nicht in Python.** Der Wächter ist das Einzige, was
funktionieren muss, wenn alles andere kaputt ist. Ein Python-Programm setzt
voraus, dass der Interpreter heil ist: keine halb angewandte `apt`-Transaktion,
kein zerschossener `python3`-Symlink nach einem Versionssprung, keine
beschädigte `.pyc`-Datei auf einer sterbenden Karte. Ein statisch gebundenes
Binärprogramm kennt diese Fehlerklasse nicht. Go statt Rust, weil die
Standardbibliothek entscheidet: Zeitrechnung und Dateiarbeit sind enthalten,
Kreuzübersetzung für `arm64`/`amd64` braucht kein Zusatzwerkzeug, und Rusts
Stärke -- Sicherheit beim Verarbeiten fremder Daten -- trägt hier wenig, weil
der Wächter nur eine Datei seines eigenen Geschwisterprozesses liest und
`systemctl`/die Container-Laufzeit aufruft. Kein Netz, keine fremden Eingaben.

**Kein Docker-Abbild.** Er läuft außerhalb der Containerlaufzeit -- er startet
und stoppt Container und muss gerade dann da sein, wenn die nicht laufen.
Ausgeliefert wird er mit dem vorbereiteten Systemabbild (`abbild/`) und einer
systemd-Einheit (`thermoctl-waechter.service`), aktualisiert über die
Paketverwaltung des Betriebssystems -- bewusst außerhalb des Fleet-Dienstes.

## Bedingungen (Abschnitt 18.3, wörtlich übernommen)

- **`go.mod` ohne eine einzige Abhängigkeit.** Insbesondere nicht das
  Docker-SDK -- die Container-Laufzeit wird über ihr Kommandozeilenwerkzeug
  oder über `systemctl` angesprochen.
- **Statisch gebaut** (`CGO_ENABLED=0`), je ein Binärprogramm für `arm64` und
  `amd64`, mit Prüfsumme, in der CI erzeugt und ins Abbild gelegt. Auf dem
  Gerät wird nichts übersetzt.
- **Unter 300 Zeilen.** Wächst er darüber hinaus, stimmt der Zuschnitt nicht.
- Eigene CI-Spur (`.github/workflows/go.yml`): `go vet`, `go test`, Bau für
  beide Architekturen. Die Python-Spur (`ci.yml`) bleibt unverändert.

## Wer lädt, und wer tauscht (Abschnitt 17)

Der **Agent** (Python, `agent/`) lädt ein neues Abbild herunter, prüft dessen
Digest gegen die fest eingebauten Quellen und schreibt danach beide Digests in
die Zustandsdatei (`zustand.go`). Der **Wächter** sieht nie ein Netzwerk,
kennt keine Registry und prüft keine Signaturen -- er kennt zwei lokal
vorhandene Digests und eine Frage: *Hat der Agent innerhalb der Frist „ich bin
gesund" gesagt?* Diese Trennung ist der Grund, warum die Fähigkeiten, die der
Wächter **nicht** hat, genauso wichtig sind wie die, die er hat: Ein Wächter
mit Registry-Zugang wäre ein zweiter Pfad, auf dem fremder Code aufs Gerät
kommt.

## Der Vertrag mit dem Agenten

Zwei Dateien, beide zeilenbasiert (`SCHLUESSEL=WERT`, wie eine
systemd-Umgebungsdatei), **kein JSON** -- die Begründung dafür steht als
Kommentar in `zustand.go`, an der Stelle, an der es zählt, damit sie nicht
verloren geht, wenn jemand das Format später "vereinfachen" will:

- `zustand.go`: liest die von Python (`agent.schleife.waechter_zustand_melden`)
  geschriebene Zustandsdatei (`gewuenscht`, `bewaehrt`, `seit`).
- `gesundmeldung.go`: liest die vom laufenden Agenten periodisch geschriebene
  Gesundmeldung (ein Unix-Zeitstempel).

`pruefe_vertrag.sh` ist der sprachübergreifende Vertragstest aus Abschnitt
18.3: Python schreibt, das gebaute Go-Binärprogramm liest im Prüfmodus
(`-pruefmodus`), das Ergebnis wird verglichen. Er läuft in
`.github/workflows/go.yml` -- siehe dessen Kommentar für die Begründung, warum
er dort und nicht in `pytest` oder `go test` allein steht.

## Stand dieses Gerüsts

`zustand.go` und `gesundmeldung.go` sind echt umgesetzt (reines Parsen, keine
Fremdpakete). `wache.go` (Agent-Ende erkennen, Digest starten, auf
Gesundmeldung warten, auf `bewaehrt` zurücksetzen) ist Gerüst -- jede Funktion
gibt einen Fehler mit Verweis auf Abschnitt 17 zurück, keine ist umgesetzt.

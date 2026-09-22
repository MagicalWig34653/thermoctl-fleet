# Stand

Letzte Aktualisierung: 2026-09-22.

## Nur ein Gerüst

Dieses Repository enthält **keine** funktionierende Anwendung. `protokoll/` ist
vollständig (Pydantic-Modelle für Herzschlag, Befehl/Befehlsergebnis, Sollzustand,
Anmeldung). `fleet/` und `agent/` haben je ein Endpunkt- bzw. Schleifengerüst mit
`NotImplementedError` an jeder Stelle, an der Umsetzung fehlt — jede Fundstelle
trägt einen Verweis auf den Abschnitt in `docs/spezifikation.md`.

Die vollständige Umsetzung ist nicht begonnen. Was als Nächstes ansteht, richtet
sich nach der Reihenfolge in Abschnitt 11 der Spezifikation:

1. Webhook-Empfänger `POST /v1/ereignisse` in `fleet/` fertigstellen (Anmeldungsprüfung,
   Ablage, Alarmauswertung) — **ohne** Änderung an thermoctl.
2. `POST /v1/herzschlag` fertigstellen, Alarmierung bei Ausbleiben (Abschnitt 8).
3. Weboberfläche „Das Haus" und „Eine Wohnung" (Abschnitt 9).
4. SSE-Kanal `GET /v1/befehle` und die vier Stufe-1-Befehle im Melder.
5. Sollzustandsabgleich (Abschnitt 13) und Sicherung/Wiederherstellung (Abschnitt 15).

## Offene Punkte aus dem Zuschnitt des Gerüsts

- **`/v1/ereignisse` hat kein festgelegtes Nutzlastschema.** Die Spezifikation
  (Abschnitt 11) beschreibt nur, dass die sechs vorhandenen thermoctl-Störungsmeldungen
  hier auflaufen, "mit Wohnung im Text" — ohne Beispiel. `protokoll/ereignisse.py`
  enthält deshalb eine **Annahme**, angelehnt an `OffeneStoerung`, keine wörtliche
  Übernahme. Vor der Umsetzung klären: das tatsächliche Nutzlastformat von thermoctls
  Störungs-Webhooks (siehe thermoctl, „Einstellungen").
- **Abschnitt 17 (Wächter/Agent-Aktualisierung)** beschreibt einen dritten,
  eigenständigen Prozess auf der Basisstation — den Wächter, "wenige hundert Zeilen,
  ein systemd-Dienst", der **nicht** über den Fleet-Dienst, sondern mit dem
  Betriebssystem ausgeliefert wird. Er ist bewusst kein drittes Docker-Abbild dieses
  Repositories (die Spezifikation sagt das ausdrücklich) und hat hier deshalb noch
  keinen Platz — wohin er gehört (eigenes Repository? ein Unterordner unter `agent/`,
  der nicht mitverpackt wird?), ist nicht entschieden.
- **Token-Format nicht als Modell.** `melder_<wohnung>_<zufall>` (Abschnitt 4) ist
  bewusst kein Pydantic-Modell mit Beispielwert — ein Repository-taugliches Beispiel
  sähe wie ein echtes Geheimnis aus. Wer das Format prüfen will, tut das über eine
  eigene, secret-freie Validierungsfunktion, nicht über ein Modell mit Default.
- **Protokollversion (`protokoll.version.PROTOKOLLVERSION`)** ist angelegt, wird aber
  von keinem Endpunkt und keinem Modell ausgewertet — Verträglichkeitsprüfungen
  zwischen unterschiedlichen Fassungen von `fleet` und `agent` sind noch nicht
  entworfen.

## CI

`ci.yml` prüft ruff, mypy und pytest gegen Python 3.13 und 3.14, ohne
Datenbankdienst — das Gerüst legt nichts ab. `docker.yml` baut bei `v*`-Tags zwei
Abbilder (`thermoctl-fleet`, `thermoctl-agent`) für `linux/amd64` und `linux/arm64`
nach ghcr.io, und bei jedem Pull Request zur Bauprobe ohne Veröffentlichung.

#!/usr/bin/env bash
# Der sprachübergreifende Vertragstest aus Abschnitt 18.3: "Python schreibt
# die Zustandsdatei, Go liest sie. Zwei Sprachen, die sich kein Modell teilen
# können, prüfen denselben Vertrag härter als zwei Python-Module, die
# womöglich gemeinsam falsch liegen."
#
# Genau das führt dieses Skript aus, nicht als Behauptung, sondern als
# echter Ablauf: (1) den Wächter bauen, (2) Python die Zustandsdatei mit
# `agent.schleife.waechter_zustand_melden` schreiben lassen -- derselbe Code,
# der später auf dem echten Gerät läuft, keine Testattrappe --, (3) den
# gebauten Wächter im Prüfmodus (`-pruefmodus`) lesen lassen, (4) das
# Ergebnis mit den ursprünglichen Werten vergleichen.
#
# Läuft in .github/workflows/go.yml, nicht in ci.yml: Die Python-Spur bleibt
# unverändert (Abschnitt 18.3, Bedingungen), und dieser Test braucht sowohl
# Go als auch Python -- er gehört zur Go-Spur, weil sie ihn zusätzlich zur
# reinen Python-Installation braucht, nicht umgekehrt.
set -euo pipefail

hier="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wurzel="$(cd "$hier/.." && pwd)"
arbeitsverzeichnis="$(mktemp -d)"
trap 'rm -rf "$arbeitsverzeichnis"' EXIT

zustandsdatei="$arbeitsverzeichnis/zustand.env"
erwartet_gewuenscht="sha256:$(printf 'a%.0s' {1..64})"
erwartet_bewaehrt="sha256:$(printf 'b%.0s' {1..64})"

echo "1. Wächter bauen ..."
binaerprogramm="$arbeitsverzeichnis/thermoctl-waechter"
(cd "$hier" && go build -o "$binaerprogramm" .)

echo "2. Python schreibt die Zustandsdatei (agent.schleife.waechter_zustand_melden) ..."
PYTHONPATH="$wurzel" python3 -c "
from pathlib import Path
from agent.schleife import waechter_zustand_melden

waechter_zustand_melden(
    Path('$zustandsdatei'),
    gewuenscht='$erwartet_gewuenscht',
    bewaehrt='$erwartet_bewaehrt',
)
"

echo "3. Wächter liest im Prüfmodus ..."
ausgabe="$("$binaerprogramm" -pruefmodus -datei "$zustandsdatei")"
echo "$ausgabe"

echo "4. Vergleichen ..."
if ! grep -qx "GEWUENSCHT=$erwartet_gewuenscht" <<<"$ausgabe"; then
    echo "FEHLER: GEWUENSCHT stimmt nicht überein." >&2
    exit 1
fi
if ! grep -qx "BEWAEHRT=$erwartet_bewaehrt" <<<"$ausgabe"; then
    echo "FEHLER: BEWAEHRT stimmt nicht überein." >&2
    exit 1
fi

echo "Vertragstest bestanden: Python geschrieben, Go gelesen, Werte identisch."

// Gemeinsames Zeilenformat für Zustandsdatei und Gesundmeldung (Abschnitt 17,
// 18.3, 22.3): "Schluessel=Wert" je Zeile, "#" leitet einen Kommentar ein.
package main

import (
	"bufio"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// liesSchluesselWertZeilen liest r zeilenweise "Schluessel=Wert" in eine Map.
// Leere Zeilen und Kommentare werden übersprungen; eine Zeile ohne "=" ist
// ein Fehler. datei dient nur der Fehlermeldung.
func liesSchluesselWertZeilen(r io.Reader, datei string) (map[string]string, error) {
	werte := map[string]string{}
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		zeile := strings.TrimSpace(scanner.Text())
		if zeile == "" || strings.HasPrefix(zeile, "#") {
			continue
		}
		schluessel, wert, gefunden := strings.Cut(zeile, "=")
		if !gefunden {
			return nil, fmt.Errorf("%s: Zeile ohne '=': %q", datei, zeile)
		}
		werte[strings.TrimSpace(schluessel)] = strings.TrimSpace(wert)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return werte, nil
}

// parseOptionalerZeitstempel liest ein Unix-Sekunden-Feld, leer -> 0.
func parseOptionalerZeitstempel(wert, feldname, datei string) (int64, error) {
	if wert == "" {
		return 0, nil
	}
	zeitstempel, err := strconv.ParseInt(wert, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s: %q ist kein Unix-Zeitstempel: %w", datei, feldname, err)
	}
	return zeitstempel, nil
}

// Die Gesundmeldung (Abschnitt 17, Schritt 5). Zeilenbasiert wie die
// Zustandsdatei, nicht ein einzelner Zeitstempel (Abschnitt 22.3,
// nachträglich festgelegt): `zeitpunkt=`, `digest=`, `fassung=`. Der Gewinn
// ist `digest`, der **laufende** Digest des schreibenden Container-Standes
// -- der Wächter sieht damit, dass das Richtige lebt, nicht nur irgendetwas.
package main

import (
	"fmt"
	"io"
	"os"
	"strconv"
)

// Gesundmeldung ist der geparste Inhalt der Gesundmeldungsdatei.
type Gesundmeldung struct {
	Zeitpunkt int64
	Digest    string
	Fassung   string
}

// ParseGesundmeldung liest eine Gesundmeldungsdatei aus r. Unbekannte
// Schlüssel werden ignoriert, nicht abgelehnt (wie bei der Zustandsdatei).
func ParseGesundmeldung(r io.Reader) (Gesundmeldung, error) {
	werte, err := liesSchluesselWertZeilen(r, "gesundmeldung")
	if err != nil {
		return Gesundmeldung{}, err
	}
	if werte["zeitpunkt"] == "" {
		return Gesundmeldung{}, fmt.Errorf("gesundmeldung: 'zeitpunkt' fehlt oder ist leer")
	}
	zeitpunkt, err := strconv.ParseInt(werte["zeitpunkt"], 10, 64)
	if err != nil {
		return Gesundmeldung{}, fmt.Errorf("gesundmeldung: 'zeitpunkt' ist kein Unix-Zeitstempel: %w", err)
	}
	return Gesundmeldung{Zeitpunkt: zeitpunkt, Digest: werte["digest"], Fassung: werte["fassung"]}, nil
}

// LiesGesundmeldung öffnet pfad und parst ihn.
func LiesGesundmeldung(pfad string) (Gesundmeldung, error) {
	datei, err := os.Open(pfad)
	if err != nil {
		return Gesundmeldung{}, err
	}
	defer datei.Close()
	return ParseGesundmeldung(datei)
}

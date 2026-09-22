// Die Gesundmeldung: der zweite Teil des Vertrags mit dem Agenten (Abschnitt
// 17, Schritt 5): "Der neue Agent muss binnen 10 Minuten seinen Selbsttest
// bestehen und regelmäßig eine Gesundmeldung in eine lokale Datei schreiben."
//
// Dieselbe Formbegründung wie bei der Zustandsdatei (siehe zustand.go): eine
// einzelne Zeile mit einem Unix-Zeitstempel statt JSON, damit ein künftiger
// Wächter in einer anderen Sprache sie ohne Zusatzpaket lesen kann. Die
// Spezifikation legt dieses Dateiformat nicht wörtlich fest (anders als bei
// der Zustandsdatei) -- diese Wahl ist eine Annahme des Gerüsts in
// Konsistenz mit ihr, keine belegte Festlegung.
package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// LiesGesundmeldung liest den zuletzt gemeldeten Unix-Zeitstempel.
func LiesGesundmeldung(pfad string) (int64, error) {
	inhalt, err := os.ReadFile(pfad)
	if err != nil {
		return 0, err
	}
	zeile := strings.TrimSpace(string(inhalt))
	zeitpunkt, err := strconv.ParseInt(zeile, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("gesundmeldung: kein Unix-Zeitstempel: %w", err)
	}
	return zeitpunkt, nil
}

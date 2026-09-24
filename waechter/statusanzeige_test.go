package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLedVorhandenFehlend(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "brightness")
	if LedVorhanden(pfad) {
		t.Fatal("LedVorhanden meldet true fuer eine nicht existierende Datei")
	}
}

func TestLedVorhandenVorhanden(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "brightness")
	if err := os.WriteFile(pfad, []byte("0"), 0o644); err != nil {
		t.Fatalf("Testdatei anlegen fehlgeschlagen: %v", err)
	}
	if !LedVorhanden(pfad) {
		t.Fatal("LedVorhanden meldet false fuer eine vorhandene Datei")
	}
}

// TestLedMusterSetzenOhneDateiKeinFehler prueft den zentralen Punkt aus
// Abschnitt 23.3: eine fehlende LED-Datei ist kein Fehler, der Waechter
// laeuft unveraendert weiter.
func TestLedMusterSetzenOhneDateiKeinFehler(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "brightness")
	if err := LedMusterSetzen(pfad, "langsames-blinken"); err != nil {
		t.Fatalf("erwartete keinen Fehler bei fehlender LED-Datei, bekam: %v", err)
	}
}

func TestLedMusterSetzenNichtUmgesetzt(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "brightness")
	if err := os.WriteFile(pfad, []byte("0"), 0o644); err != nil {
		t.Fatalf("Testdatei anlegen fehlgeschlagen: %v", err)
	}
	if err := LedMusterSetzen(pfad, "dauerhaft-an"); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

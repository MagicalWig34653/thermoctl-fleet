package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLiesGesundmeldung(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "gesundmeldung")
	if err := os.WriteFile(pfad, []byte("1790000123\n"), 0o600); err != nil {
		t.Fatalf("Vorbereitung fehlgeschlagen: %v", err)
	}

	zeitpunkt, err := LiesGesundmeldung(pfad)
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if zeitpunkt != 1790000123 {
		t.Errorf("zeitpunkt = %d, erwartet 1790000123", zeitpunkt)
	}
}

func TestLiesGesundmeldungLehntUngueltigenInhaltAb(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "gesundmeldung")
	if err := os.WriteFile(pfad, []byte("kein-zeitstempel\n"), 0o600); err != nil {
		t.Fatalf("Vorbereitung fehlgeschlagen: %v", err)
	}

	_, err := LiesGesundmeldung(pfad)
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestLiesGesundmeldungFehltDatei(t *testing.T) {
	_, err := LiesGesundmeldung(filepath.Join(t.TempDir(), "fehlt"))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}
